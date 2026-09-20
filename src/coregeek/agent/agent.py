"""`Agent` —— 单实例的解题智能体：一个进程一个，跨回合活着，跨回合状态全在实例上。

`_sop`（流程表，整场存活）、`_news_digest`/`_price_hints`（新闻指纹与价格期望）、`_context`
（任务内会话，题目变了即换新）。失败退化路径：状态丢了只影响 prompt 的内容、不碰红线
（会话丢了 ⇒ 退化成单轮提问）；判题器逐回合同步请求 ⇒ 不加锁。SOP 不按任务分区、会话按
任务分区（身份 = 题目原文）；探明的沙箱清单（在 `cmd_explore` 上）同 SOP 一样整场存活。
`planner.task_channel` 每次都用包根那个 `AGENT`。
"""

import logging
from collections.abc import Callable

from . import cmd_explore
from .chat import is_prices_reply, is_summary_reply, strip_answers
from .context import Context
from .prompt import gen_compression_prompt, gen_news_prompt, gen_system_prompt
from .tools import pyexec
from .tools.cmd import executeCmd
from .tools.sop import store

LOGGER = logging.getLogger(__name__)

#: 【工具调用】那行里每个参数值截到多少字（拍）。原文在任务行的「上一轮模型回复」里，这一行是
#: 索引 ⇒ 值再长也不影响可读性，只是给日志一个上界（`SOP2Prompt` 的 `sop` 上限正好 1000）。
ARGS_LOG_MAX = 1000


def _call_text(params: list[tuple[str, str]]) -> str:
    """`[(参数名, 原文), …]` 打成一行的 `名=值`，没有参数打「无参数」。

    超 `ARGS_LOG_MAX` 截断留痕（与 `utils._clip` 同形；agent 是叶子包，规则各存一份）。
    """
    parts = []
    for name, value in params:
        value = str(value)
        if len(value) > ARGS_LOG_MAX:
            value = value[:ARGS_LOG_MAX] + f"…（共 {len(value)} 字）"
        parts.append(f"{name}={value}")
    return "，".join(parts) or "无参数"


def _probed_note(path: str) -> str:
    """未命中时回给 LLM 的说明：`path` 是枚举值，可选的只有探明过的那几份。

    三种未命中各说各的话：清单空着（探查还没取回东西 —— "我们还没摸过"不等于"沙盒里没有"）、
    文件名撞了（要它改写成完整路径）、其余（让它回去照抄，或改用 executeCmd）。
    清单不在这里重抄 —— 它挂在 `readSandboxFile` 的描述里（`Agent.prompt_tools` 现挂），
    与这条说明同处一份 prompt；撞名那一支只列**撞上的那几份**，那是回答"你指的是哪一份"。
    """
    if not cmd_explore.known_paths():
        return (
            f"{path} 不在可选清单里 —— 沙盒里还没有探明任何文件（探查可能还没跑完）。"
            "要读文件请用 executeCmd 自己找、自己读。"
        )
    hits = cmd_explore.matches(path)
    if len(hits) > 1:
        return (
            f"{path} 对上了不止一份已探明的文件：{'、'.join(hits)}。"
            "`path` 请照抄完整路径 —— 只写文件名时它定不下是哪一份。"
        )
    return (
        f"{path} 不在可选清单里：`path` 只能取 `readSandboxFile` 描述里列出的那些完整路径"
        "或文件名（照抄）；清单以外的文件请用 executeCmd 自己找、自己读。"
    )


class Agent:
    """会用工具解题的智能体。建一个就够（`coregeek.agent.AGENT`）。"""

    def __init__(self) -> None:
        #: 「沉淀的 SOP」—— 整场存活的流程表 `{流程名: 正文}`
        #: （同名覆盖、异名追加、条数上限）。重启清空。
        self._sop: dict[str, str] = {}
        #: 价格期望与新闻指纹。退化路径：期望错了 = 采矿偏好偏一天，不碰红线；指纹丢了
        #: = 重问一次新闻（额度 3/日）。
        self._news_digest = ""
        self._price_hints: dict[str, float] = {}
        #: 任务内的会话上下文。题目变了即换新；任务结束不清（死会话，下场换题自然被替）。
        self._context: Context | None = None
        #: 工具名 → (实现, 给 LLM 看的描述, 参数表 `((参数名, 用途), …)`)。描述措辞直接决定
        #: 调用正确率（`prompt.gen_all_tool_prompt` 由表生成、不手写第二份）；参数表同时是
        #: `tool_call` 的调度签名 —— 一张表两处用，描述与调度不会分家。顺序即 prompt 里的
        #: 顺序。表必须由实例构造：`SOP2Prompt` 写的是 `self._sop` ⇒ 只能是绑定方法。
        self._tools: dict[
            str, tuple[Callable[..., str], str, tuple[tuple[str, str], ...]]
        ] = {
            "executeCmd": (
                executeCmd,
                """
[用途]在判题器的沙盒里执行一条基础 shell 与 python 指令。用于所有必须与沙盒环境交互的操作，包括：
- 探查沙盒环境 
- 查找尚未探明位置的文件 
- 读取未列入沙盒知识清单的文件 
- 执行任务命令、脚本和程序 
- 调用沙盒中的本地接口 
- 修改任务文件或环境 
- 调试程序 
- 运行任务要求的验证脚本 
- 验证任务是否完成

[重要成本]一次 executeCmd 执行会消耗两个回合，因此它是高成本工具。如果任务不需要访问沙盒环境，不要使用它。

[使用原则]
1. 只有需要访问、执行或修改沙盒环境时才使用本工具。
2. 能使用 readSandboxFile 直接读取的已探明文件，不要用 executeCmd 读取。
3. 纯计算、字符串处理、JSON 解析、结果比较等不需要访问沙盒的工作，使用 python_exec，不要使用 executeCmd。
4. 一次 executeCmd 尽可能完成多个连续操作，不要把本可以放在同一条命令中的操作拆成多次调用。
5. 可以在一条命令中使用 shell 的变量、if、for、&&、||、; 等组合操作完成查找、读取、判断、尝试和验证。
6. 如果下一步完全取决于当前命令的输出，优先考虑能否把判断逻辑直接写进同一条命令。
7. 如果需要尝试多个候选值，优先在一次命令中批量尝试，并给每次尝试输出清晰标签。
8. 任务书明确指定脚本或验证方式时，优先执行指定脚本，不要自行创造另一套验证方式。

[输出特点]命令执行结果会在后续回合返回。因此命令输出必须便于下一回合理解：
- 给不同尝试加明确标签
- 输出关键结果
- 不要产生大量无关输出

[边界]
- 不能访问外网。
- 不能用它代替 python_exec 做纯计算。
- 不能因为“方便”而重复读取已经通过 readSandboxFile 可以直接获得的文件。
                """,
                (("cmd", "需要在沙盒中执行的完整命令原文"),),
            ),
            "readSandboxFile": (
                self.read_sandbox_file,
                """
[用途]读取一份已经通过沙盒探查确认存在的文件，并在当前回合直接返回文件正文。

[适用场景]当目标文件已经出现在本工具描述中的“已探明文件清单”里，并且当前只需要读取其内容时，优先使用本工具。

[成本优势]本工具比 executeCmd 省一个回合。因此：
- 已知文件 + 只需要读取 → 优先使用本工具 
- 已知文件 + 还需要执行命令/修改/处理环境 → 使用 executeCmd

[路径规则]path 必须对应本工具描述中已经列出的文件。可以：
1. 传入清单中的完整路径；
2. 传入清单中的文件名。

如果同名文件不止一个，必须提供完整路径。不要自行猜测、拼接或修改目录。

[重要说明]
1. 清单之外的文件不会由本工具主动搜索。
2. 清单中暂时没有某个文件，不代表沙盒中不存在该文件。
3. 如果目标文件不在清单中，使用 executeCmd 查找并读取。
4. 沙盒知识清单会随着环境探查逐步增加。
5. 已经探查确认存在的文件路径可以直接信任，不需要重复 find。

[边界]本工具只负责读取文件正文。不能用于：
- 查找未知文件
- 执行命令
- 修改文件
- 执行脚本
- 调用接口
                """,
                (("path", "已探明文件的全路径、或它的文件名（只能取本工具描述里列出的那些）"),),
            ),
            "python_exec": (
                self.python_exec,
"""
[用途]
在本地即时执行一段纯计算 Python，并在当前回合直接返回结果。

[优先使用场景]
以下任务优先使用本工具，不要占用昂贵的沙盒执行：
- 数值计算 
- 字符串处理 
- JSON 解析与构造 
- 数据转换 
- 多个结果的比较 
- 正则处理 
- 构造命令参数 
- 构造下一条命令 
- 根据工具返回结果进行计算 
- 对候选结果进行筛选、排序、匹配

[成本优势]
本工具：
- 不访问判题器沙盒，仅需一个回合执行
- 不需要等待沙盒回执 

- 适合处理不需要环境访问的中间计算：
> “需要计算”用 python_exec；
> “需要访问沙盒”用 executeCmd；
> “只需要读取已知文件”用 readSandboxFile。

[严格限制]
本工具只能进行纯计算。禁止：
- 读取或写入文件 
- 访问网络 
- 访问环境变量 
- 访问操作系统环境 
- 执行 shell 命令 
- 读取沙盒任务文件 
- 修改沙盒环境
如果需要读取任务文件，必须使用 readSandboxFile 或 executeCmd。

[允许的 import]只能math cmath decimal fractions statistics itertools functools collections heapq bisect array json re string datetime random 禁止其他 import。

[执行限制]执行时间超过 2 秒会被终止。使用 print 输出结果，或者直接写一个表达式让其返回结果。

[推荐用法]如果上一轮 executeCmd 得到了大量结果：
1. 不要再次调用 executeCmd 做解析；
2. 将已有结果交给 python_exec 进行解析、比较、转换；
3. 得出下一步需要的参数后，再决定是否调用沙盒工具。
""",
                (("code", "要执行的 Python 代码原文"),),
            ),
            "SOP2Prompt": (
                self.SOP2Prompt,
"""
[用途]
将当前任务中已经验证过、并且未来同类任务可以复用的知识沉淀为 SOP。

SOP 的目标不是记录本次任务，而是让下一次遇到同类任务时能够少做探索。

[什么时候使用]
只有满足以下条件时才应该调用：
1. 当前任务已经获得新的、可复用的知识；
2. 该知识未来可能用于解决同类任务；
3. 该知识尚未存在于已有 SOP 中，或者需要修正已有 SOP；
4. 当前任务已经足以确认该知识，而不是模型猜测。

[可以沉淀的内容]
一、通用解决流程
例如：
- 如何获得任务输入 
- 操作步骤 
- 工具调用顺序 
- 如何处理返回值 
- 如何验证成功 
- 如何得到最终答案
二、经过实际执行确认的环境知识
例如：
- 实际接口路径 
- HTTP 方法 
- 参数名称 
- 参数类型 
- 参数含义 
- 返回值结构 
- 文件路径 
- 工具特殊行为 
- 错误信息揭示的约束 
- 文档与实际行为的差异

[不能沉淀]
不要把以下一次性信息写入通用 SOP：
- 当前任务的具体目标值 
- 当前任务的 token 
- 当前任务的临时结果 
- 当前任务产生的临时文件 
- 仅本次有效的参数值 
- 一次性的中间状态 
- 未经验证的猜测 
- 完整的探索日志

判断标准：
> 下一次遇到一个目标值不同、但本质相同的任务时，这条 SOP 能否直接减少至少一步探索？不能 → 不沉淀。能 → 考虑沉淀。

[泛化要求]
- name 必须描述“一类问题”，不能描述某一次任务。
    - 正确：“订去某地的机票的流程” 
    - 错误：“订去上海的机票” 
- sop 正文也必须泛化。
    - 正确：“读取任务获得目的地，然后调用订票接口，将目的地作为目标参数传入，成功后从返回结果中获取 token。” 
    - 错误：“读取上海，然后调用接口传入上海，得到 tk_xxxxx。”
    
[实际结果优先]
如果文档、旧 SOP 与实际执行结果冲突：
- 以实际执行结果为准；
- 如果这个差异具有未来复用价值，应更新 SOP。
例如：
文档写参数为 destination，实际执行确认必须使用 target，则应该沉淀实际可用的 target。

[去重与更新]调用前必须检查已有 SOP。
如果已经存在相同知识：
- 不重复创建。
如果新结果证明旧 SOP 已经过时：
- 使用相同 name 更新旧 SOP。
如果属于完全不同的问题类型：
- 使用新的 name 创建新的 SOP。同名会覆盖旧条目，不同名会追加新条目。

[调用后的行为]
SOP2Prompt：
- 只负责沉淀；
- 不执行任务；
- 不产生新的命令；
- 不返回执行结果；
- 不需要等待回执。
因此可以在同一个回合中：1. 调用 SOP2Prompt 沉淀经验；2. 在工具块之后立即输出最终 answer。

[重要]
SOP 中不要出现：<answer> </answer> 如果需要描述答案格式，应写成："将最终结果放入 answer 标签" 而不是直接写 XML 标签。
""",
                (
                    ("name", "泛化后的问题类型名称，例如“订去某地的机票的流程”"),
                    ("sop", "该类问题的通用解决流程，以及经过实际执行确认的接口、路径、参数、返回值等环境知识"),
                ),
            ),
        }

    def chat(self, request: str, *, result: str = "", retry: str = "") -> str:
        """组装累积的会话发给判题器的 LLM。`request` = 题目原文；同一道题续上旧会话、换题
        换新。首问 = 题目（构造即问）；回灌轮 = 结果/纠错（`feed`）；无新内容的重问轮 = 一句
        「请继续。」（`nudge`）—— 判题器的 LLM 是黑盒，会话停在它自己的输出上是个含糊指令。
        返回值是标准 messages JSON（`[{role, content}]`）。

        system（`prompt.py` 的段模板）每次现刷：SOP 是活的，任务进行中沉淀的下一轮就得看得见
        —— 那是 `SOP2Prompt` "调用成功"的回执（它不产出命令）；沙盒探查摸到的路径同理，
        下一轮就现挂在 `readSandboxFile` 的描述里（`prompt_tools`）。
        """
        fresh = self._context is None or self._context.task != request
        if fresh:
            # 换题 ⇒ 新会话。粘住的回执也照样 feed（照样回灌）。
            self._context = Context(request)
        if result or retry:
            self._context.feed(result, retry)
        elif not fresh:
            self._context.nudge()
        self._context.system = gen_system_prompt(self.prompt_tools(), self._sop)
        return self._context.render()

    def prompt_tools(self) -> dict:
        """这一轮给 LLM 看的工具表：`readSandboxFile` 的描述尾部**现挂**探明的路径清单。

        工具块**恒在**（第 107 步）：它的描述正文里点过别的工具（"已知文件用 readSandboxFile"），
        藏起来就成了一条悬空指引，而它的描述本身也交代了清单为空时怎么办。清单那几行只在探明过
        之后才挂 —— `path` 是枚举值，一份都没探明时挂个空清单等于暗示"沙盒里没有这些文件"
        （**"我们还没摸过"不等于"沙盒里没有"**）。探明后每行给两种写法（整条全路径、或它的
        文件名）—— 那张表就是那个 `path` 参数的合法取值表，跟着参数自己所在的那块走。

        调度那一侧不看这张表（`tool_call` 照旧认得它、给同一条不成立的结论），两处口径一致。
        """
        tools = dict(self._tools)
        paths = cmd_explore.known_paths()
        if not paths:
            return tools
        impl, desc, params = self._tools["readSandboxFile"]
        listed = "\n".join(
            f"- {path}（文件名 {cmd_explore.file_name(path)}）" for path in paths
        )
        tools["readSandboxFile"] = (
            impl,
            f"{desc}\n[已探明的文件]（`path` 只能取这些）：\n{listed}",
            params,
        )
        return tools

    def hear(self, reply: str) -> bool:
        """记下判题器 LLM 这回合的回复（`planner.task_channel` 每回合都调 —— 发命令/交答案
        那两轮没有 prompt，回复照样得进会话，否则回灌时它自己的命令凭空消失）。

        返回"这条算不算新的"—— `task_channel` 按它区分"这轮真说了个新命令"与"粘住的
        `llmResp` 把上一条命令又报了一遍"（后者不再发一遍：那条命令已经在沙盒里跑了）。
        还没开过会话（这道题一次都没问过）⇒ 什么都不记、算新的（无从说它粘住）。粘住的
        重复由 `Context.hear` 去重（只比最后一条消息 —— 中间隔了回执/重问就再记一次）。
        任务回复里零星的 `<summary>` 一律当普通文字记 —— 摘要的唯一来源是 `adopt_summary`。
        """
        if self._context is None:
            return True
        return self._context.hear(reply)

    def adopt_summary(self, text: str) -> None:
        """把压缩轮的摘要记进会话上下文。还没开过会话 ⇒ 忽略。

        调用方是 `task_channel` 的路由（裸 `<summary>` 回复 = 压缩请求的产物）：摘要进
        `Context.summary`、回复不进会话表 —— 它不是 LLM 在任务上说过的话。
        """
        if self._context is not None:
            self._context.summary = text

    def compression_request(self) -> str:
        """压缩轮的 prompt：独立指令 + 原始上下文全文。没开过会话 ⇒ `""`。

        发送时机 = 回合最末尾的压缩闸门（`task_channel` 判据链算完 `prompt` 还是空才发，
        只剩命令轮 —— 答案轮不压缩，压缩与 `<answer>` 互斥），模型请求永远优先。原料由
        `Context.material()` 给出：原文永久保留、压缩总从原文重来；给任务 LLM 的才是压缩后的。
        """
        if self._context is None:
            return ""
        return gen_compression_prompt(self._context.material())

    def read_sandbox_file(self, path: str) -> str:
        """读沙盒里的一份文件：`path` 是枚举值，只能取探明过的那些（`readSandboxFile` 描述
        里现挂的那份清单），写整条全路径或只写文件名都行；命中就当回合把正文送进会话、不产命令。

        不在清单里、或文件名对上不止一份 ⇒ 调用不成立（返回 `""`）＋把说明回给 LLM：
        **绝不替它往沙盒发 `cat`** —— 它编出来的路径那趟必然报错，白烧一个沙盒往返还引它接着猜
        下一个。清单以外的文件用 executeCmd 自己读（与摘要段那条同一个道理：不能把"我们还没摸过"
        说成"沙盒里没有"）。返回值恒为 `""`：这个工具只会把东西送进会话，从不产出命令。
        """
        body = cmd_explore.body_of(path)
        if not body:
            # 对上几条决定它是"没这份"还是"文件名撞了"，回给 LLM 的话不同（`_probed_note`）
            hits = cmd_explore.matches(path)
            LOGGER.info("【沙盒文件】：%s 这次调用不成立（对上 %d 条探明的路径）", path, len(hits))
            if self._context is not None:
                self._context.tool_output(_probed_note(path), "【沙盒文件：这次调用不成立】")
            return ""
        if self._context is not None:
            self._context.tool_output(body, f"【沙盒文件 {path} 的正文（本地已探明）】")
        LOGGER.info("【沙盒文件】：%s 本地取回 %d 字", path, len(body))
        return ""

    def python_exec(self, code: str) -> str:
        """本地即时计算：`pyexec.run` 跑代码，产出当场记进会话（`tool` 消息，跟着 LLM 那条
        调用走），返回 `""` —— 不产命令。重问的 prompt 窗口里它看得见自己的调用与产出，
        下一回合就能作答，比 `executeCmd` 省一整个沙盒往返。

        与 `executeCmd` 的分工（两边的描述里都写了）：那是判题器沙盒（能看任务文件、一回合
        往返、限 15 秒）；这里是我们进程里的纯计算（即时、看不见沙盒、安检 + 2 秒超时）。
        没开会话时产出丢弃（任务已结束的迟到回复，无害）。
        """
        output = pyexec.run(code)
        if self._context is not None:
            self._context.tool_output(output, "【本地 python 的执行结果（原文）】")
        return ""

    def news_question(self, news: str) -> str:
        """没任务时的新闻查价 prompt。同一份 news 只问一次（指纹去重 —— 任务线之外每游戏日
        只有 3 次额度）；news 空 / 指纹没变 ⇒ `""`。指纹在发问时就记下：判题器不答也只是
        "不再问了"，不重发同一份。
        """
        if not news:
            return ""
        digest = f"{len(news)}:{news[:64]}:{news[-32:]}"
        if digest == self._news_digest:
            return ""
        self._news_digest = digest
        return gen_news_prompt(news)

    def adopt_price_hints(self, hints: dict[str, str]) -> None:
        """新闻查价回复（裸 `<prices>` 块）→ 价格期望表。

        粗粒度方向：up ×2 / down ×0.5 / flat ×1 —— 只修正挖矿性价比的排序，payload 的实时
        收购价每回合照读（期望是叠加项，不是替代）。
        """
        factor = {"up": 2.0, "down": 0.5, "flat": 1.0}
        self._price_hints = {kind: factor.get(d, 1.0) for kind, d in hints.items()}

    def price_hint(self, kind: str) -> float:
        """矿种的新闻期望系数。没问过新闻 ⇒ 1.0（无修正）。"""
        return self._price_hints.get(kind, 1.0)

    def tool_call(self, tool_name: str, params: list[tuple[str, str]]) -> str:
        """顶层调度入口：按名字调工具，返回要放进响应顶层 `executeCmd` 的那条命令。

        `params` 是 `tool_of` 解析出来的 `[(参数名, 原文), …]`，只收具名参数：带名的按名对，
        认不出的名字忽略（不为一个编造的名字作废整次调用）；声明了的参数一个不少、值是非空
        字符串才放行，最后 `impl(**resolved)` —— 无参数工具就是 `impl()`。

        "返回值即命令"是铁律：`""` = 这个工具不产出命令（`SOP2Prompt`）/ 调用不成立（未知
        工具、形状不对、缺参数、空白值），下游不需要区分，全落到重问。绝不抛异常（它跑在
        `app.handle` 的 `try` 里，抛出去会把整回合所有角色的指令一起带走）。边界校验只在
        这一处：这是唯一一个由外部字符串驱动的入口。

        四条出口各打一条 `【工具调用】`（含"调用不成立"那三条 —— 那是 LLM 白等一回合的唯一
        线索，回复原文在任务行里但看不出它没发出去）。
        """
        entry = self._tools.get(tool_name)
        if entry is None:
            LOGGER.info("【工具调用】：未知工具「%s」⇒ 调用不成立", tool_name)
            return ""
        impl, _, spec = entry
        declared = dict(spec)
        resolved: dict[str, str] = {}
        try:
            for name, value in params:
                if name in declared:
                    resolved[name] = value
                # 认不出的参数名：忽略（宽容那一侧）—— 不进 resolved，全塞给 impl 会 TypeError
        except (TypeError, ValueError):
            LOGGER.info("【工具调用】：%s ⇒ 调用不成立（参数不是「名=值」的形状）", tool_name)
            return ""  # params 不是 [(名, 文本)] 的形状
        if any(
            not isinstance(resolved.get(pname), str) or not resolved[pname].strip()
            for pname, _ in spec
        ):
            LOGGER.info(
                "【工具调用】：%s（%s）⇒ 调用不成立（声明了的参数缺了或值为空白）",
                tool_name,
                _call_text(params),
            )
            return ""
        command = impl(**resolved)
        LOGGER.info(
            "【工具调用】：%s（%s）⇒ %s",
            tool_name,
            _call_text(params),
            "命令已出" if command else "这个工具不产出命令",
        )
        return command

    def SOP2Prompt(self, name: str, sop: str) -> str:
        """把一条条目（`name` = 这一类问题的名字、`sop` = 做法与环境知识）沉淀进流程表，返回 `""`。

        条目有两类：流程（怎么做）与知识（接口怎么调、路径在哪、格式是什么），同一张表、
        类型由 `name` 约定区分（知识条目建议「接口-XX」这类名）。存储规则（单条上限、条数
        上限、同名覆盖、截断留痕、内容没变就静默）在 `tools/sop.py`，这里只管把新表记在自己
        身上；方法名同时是注册表里的工具名。

        答案不归这个工具管：同轮作答交给 `chat.answer_of`（答案写在工具块外的 `<answer>` 里），
        这一回合算不算作答由那个谓词判，两条都走不通就落重问，而沉淀已经落库。`sop` 里成对的
        `<answer>…</answer>` 在入库前挖掉（`chat.strip_answers`，那段正文讲的往往正是"答案
        怎么写"）—— 挖掉、不是作废整次调用。空文本 = 删掉那条（只在直接调用时可达）。
        """
        sop, stripped = strip_answers(sop)
        self._sop = store(self._sop, name, sop, stripped=stripped)
        return ""

    @property
    def sop(self) -> dict[str, str]:
        """现在的流程表 —— 「沉淀的SOP」段的填充值。没沉淀过 ⇒ 空 dict。"""
        return self._sop

    def reset(self) -> None:
        """清空全部跨回合状态（流程表、新闻指纹、价格期望、会话）。只给用例用 —— 单实例是
        模块级的，同一个测试进程里会跨用例串味。

        不复用 `SOP2Prompt("名", "")`：那个会打日志，而用例的 `assertLogs` 正盯着日志。
        """
        self._sop = {}
        self._news_digest = ""
        self._price_hints = {}
        self._context = None
