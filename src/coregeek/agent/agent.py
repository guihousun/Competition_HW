"""`Agent` —— 单实例的解题智能体：一个进程一个，跨回合状态全在实例上。

`_sop`（流程表，整场）、`_news_digest` / `_price_hints`（新闻指纹与价格期望）、`_context`
（任务内会话，题目变了即换新）。状态丢了只影响 prompt 内容、不碰红线；判题器逐回合同步
请求 ⇒ 不加锁。`planner.task_channel` 每次都用包根那个 `AGENT`。
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

#: 【工具调用】那行里每个参数值截到多少字（拍的；原文在任务行的「上一轮模型回复」里）。
ARGS_LOG_MAX = 1000


def _call_text(params: list[tuple[str, str]]) -> str:
    """`[(参数名, 原文), …]` 打成一行的 `名=值`，没有参数打「无参数」；超限截断留痕。"""
    parts = []
    for name, value in params:
        value = str(value)
        if len(value) > ARGS_LOG_MAX:
            value = value[:ARGS_LOG_MAX] + f"…（共 {len(value)} 字）"
        parts.append(f"{name}={value}")
    return "，".join(parts) or "无参数"


def _probed_note(path: str) -> str:
    """未命中时回给 LLM 的说明。三种各说各话：清单空（还没摸过，不等于"沙盒里没有"）、
    文件名撞了（要它改写全路径）、其余（照抄清单，或改用 executeCmd）。清单不在这里
    重抄 —— 它挂在 `readSandboxFile` 的描述里，与这条说明同处一份 prompt。"""
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
        #: 沉淀的 SOP —— 流程表 `{流程名: 正文}`（同名覆盖、异名追加、条数上限）。
        self._sop: dict[str, str] = {}
        #: 价格期望与新闻指纹。丢了只影响采矿偏好 / 白问一次新闻，不碰红线。
        self._news_digest = ""
        self._price_hints: dict[str, float] = {}
        #: 任务内的会话上下文。题目变了即换新；任务结束不清（死会话，下场换题自然被替）。
        self._context: Context | None = None
        #: 工具名 → (实现, 给 LLM 看的描述, 参数表)。描述与调度同源这一张表（描述由
        #: `prompt.gen_all_tool_prompt` 生成，不会分家）；顺序即 prompt 里的顺序。表必须由
        #: 实例构造：`SOP2Prompt` 写 `self._sop`，只能是绑定方法。
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
        """组装累积的会话发给判题器的 LLM，返回标准 messages JSON。

        `request` = 题目原文；同一道题续上旧会话、换题换新。首问 = 题目；回灌轮 = 结果/纠错
        （`feed`）；无新内容的重问轮补一句「请继续。」。system 每次现刷：SOP 与沙盒清单
        是活的，下一轮就得看得见。"""
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
        """这一轮给 LLM 看的工具表：`readSandboxFile` 的描述尾部现挂探明的路径清单。

        工具块恒在（别的工具描述点过它的名）；清单只在探明过后挂 —— 挂空清单等于暗示
        "沙盒里没有"。每行给两种写法（全路径、文件名）。调度侧不看这张表，口径一致。"""
        tools = dict(self._tools)
        paths = cmd_explore.known_paths()
        if not paths:
            return tools
        impl, desc, params = self._tools["readSandboxFile"]
        listed = "[可选path]：" + ",".join(paths) + "\n"
        tools["readSandboxFile"] = (
            impl,
            f"{desc}\n[已探明的文件]（`path` 只能取这些）：\n{listed}",
            params,
        )
        return tools

    def hear(self, reply: str) -> bool:
        """记下判题器 LLM 这回合的回复（发命令/交答案那两轮没有 prompt，回复照样得记，
        否则回灌时它自己的命令凭空消失）。返回"这条算不算新的"：False = 空回复、或与
        最后一条 assistant 同文（粘住 —— 那条命令已在沙盒里跑，不再发一遍）。还没开过
        会话 ⇒ 什么都不记、算新的。"""
        if self._context is None:
            return True
        return self._context.hear(reply)

    def adopt_summary(self, text: str) -> None:
        """压缩轮的摘要记进会话上下文（裸 `<summary>` 回复的路由）；没开会话 ⇒ 忽略。"""
        if self._context is not None:
            self._context.summary = text

    def compression_request(self) -> str:
        """压缩轮的 prompt：独立指令 + 原始上下文全文（`Context.material`）。没开会话 ⇒ `""`。

        发送时机 = 回合末尾的压缩闸门（只剩命令轮）。"""
        if self._context is None:
            return ""
        return gen_compression_prompt(self._context.material())

    def read_sandbox_file(self, path: str) -> str:
        """读一份已探明的文件：命中 ⇒ 正文当回合进会话；不在清单里 / 撞名 ⇒ 调用不成立
        ＋ 说明回给 LLM（`_probed_note`），绝不替它往沙盒发 `cat`。返回值恒 `""`（不产命令）。"""
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
        """本地即时计算：产出当场记进会话、返回 `""`（不产命令）。比 `executeCmd` 省一整个
        沙盒往返。没开会话时产出丢弃（迟到的回复，无害）。"""
        output = pyexec.run(code)
        if self._context is not None:
            self._context.tool_output(output, "【本地 python 的执行结果（原文）】")
        return ""

    def news_question(self, news: str) -> str:
        """没任务时的新闻查价 prompt。同一份 news 只问一次（指纹去重，额度 3/日）；
        news 空 / 指纹没变 ⇒ `""`。指纹在发问时就记下：判题器不答也只是"不再问了"。"""
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

    def _note_failed_call(self, why: str) -> str:
        """把"这次调用不成立"的说明回灌进会话（LLM 侧的唯一线索）。没开会话 ⇒ 只留日志。"""
        if self._context is not None:
            self._context.tool_output(
                f"这次工具调用没有发出去，也不会有它的执行结果。{why}。",
                "【工具调用：这次调用不成立】",
            )
        return ""

    def tool_call(self, tool_name: str, params: list[tuple[str, str]]) -> str:
        """顶层调度入口：按名字调工具，返回要放进响应顶层 `executeCmd` 的那条命令。

        `params` 是 `tool_of` 解析出的 `[(参数名, 原文), …]`，只收具名参数：认不出的名字
        忽略；声明的参数一个不少、值非空才放行，最后 `impl(**resolved)`。`""` = 不产出
        命令 / 调用不成立，下游不需要区分。绝不抛异常（它跑在 `app.handle` 的 try 里，
        抛出去会把整回合所有角色的指令一起带走）。四条出口各打一条【工具调用】；不成立的
        三条另把原因回灌进会话（`_note_failed_call`）。"""
        entry = self._tools.get(tool_name)
        if entry is None:
            LOGGER.info("【工具调用】：未知工具「%s」⇒ 调用不成立", tool_name)
            return self._note_failed_call(
                f"工具「{tool_name}」不存在 —— 可用的工具只有：{'、'.join(self._tools)}。请改用其中之一"
            )
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
            return self._note_failed_call(
                "参数没有按「名=值」解析出来 —— 参数写在 <tool_param> 里、每个参数各用一对标签包裹"
            )  # params 不是 [(名, 文本)] 的形状
        missing = [
            pname
            for pname, _ in spec
            if not isinstance(resolved.get(pname), str) or not resolved[pname].strip()
        ]
        if missing:
            LOGGER.info(
                "【工具调用】：%s（%s）⇒ 调用不成立（声明了的参数缺了或值为空白）",
                tool_name,
                _call_text(params),
            )
            return self._note_failed_call(
                f"{tool_name} 缺了参数 {'、'.join(missing)}（或值是空白）"
                "—— 参数名与用途见【工具描述】里它的 Params，补齐后重新调用"
            )
        command = impl(**resolved)
        LOGGER.info(
            "【工具调用】：%s（%s）⇒ %s",
            tool_name,
            _call_text(params),
            "命令已出" if command else "这个工具不产出命令",
        )
        return command

    def reject_shape(self) -> str:
        """整条回复像工具调用、但严格解析连工具名都取不出 ⇒ 记日志 + 把说明回灌进会话。
        调用方是 `planner.task_channel`（那一轮落重问）。"""
        LOGGER.info("【工具调用】：形状没写对（取不出工具名）⇒ 这一轮落重问")
        return self._note_failed_call(
            "没能按【工具调用格式】从这条回复里解析出工具调用（取不出工具名）"
            "—— 工具名写在 <tool_name> 标签里，参数写在 <tool_param> 里、"
            "每个参数各用一对标签包裹（如 <cmd>命令</cmd>）"
        )

    def SOP2Prompt(self, name: str, sop: str) -> str:
        """沉淀一条条目（`name` = 这类问题的名字、`sop` = 做法与环境知识），返回 `""`。

        存储规则（同名覆盖、条数上限、截断留痕）在 `tools/sop.py`。条目有两类：流程与
        知识（类型由 `name` 约定区分）。`sop` 里成对的 `<answer>` 入库前挖掉
        （`chat.strip_answers`）。空文本 = 删掉那条。"""
        sop, stripped = strip_answers(sop)
        self._sop = store(self._sop, name, sop, stripped=stripped)
        return ""

    @property
    def sop(self) -> dict[str, str]:
        """现在的流程表 —— 「沉淀的SOP」段的填充值。没沉淀过 ⇒ 空 dict。"""
        return self._sop

    def reset(self) -> None:
        """清空全部跨回合状态（流程表、新闻指纹、价格期望、会话）。只给用例用 —— 单实例
        是模块级的，会跨用例串味。"""
        self._sop = {}
        self._news_digest = ""
        self._price_hints = {}
        self._context = None
