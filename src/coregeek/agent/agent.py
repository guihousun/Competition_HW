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
                "在判题器的沙盒里执行一条命令（能跑基础 shell 与 python 指令，不能访问外网）。"
                "探环境、找不知道在哪的文件、跑任务命令与脚本、调试、验证结果都用它。"
                "确定要做的几件事写在同一条命令里做完 —— 一次调用就是一个回合。",
                (("cmd", "命令原文"),),
            ),
            "readSandboxFile": (
                self.read_sandbox_file,
                "读沙盒里的一份文件：`path` 只能取本描述里列出的那些（照抄，别自己拼目录），"
                "写整条全路径、或只写它的文件名都行（文件名对上不止一份时它会要求你写全路径）。"
                "当回合就把正文送到你面前，比 executeCmd 省一个回合。"
                "清单以外的路径它不会去取 —— 那种文件用 executeCmd 自己找、自己读。"
                "清单由我们探查沙盒得出、逐回合变长：暂时没列出的文件不代表沙盒里没有。",
                (("path", "已探明文件的全路径、或它的文件名（只能取本工具描述里列出的那些）"),),
            ),
            "python_exec": (
                self.python_exec,
                "在本地即时执行一段**纯计算**的 Python：结果当回合就回到你面前"
                "（不走判题器沙盒、没有 15 秒限制，但**看不见沙盒里的任务文件**——"
                "读任务文件得走沙盒那条路）。"
                "沙盒一次往返要等一个回执、比它贵：解析、拼串、比对、构造下一条命令"
                "这类活儿放这里算，别去占沙盒。纯计算、字符串处理、JSON 解析、数据转换、"
                "拼命令参数、比较多个候选结果都归它。只允许计算：import 仅限 "
                "math/cmath/decimal/fractions/statistics/itertools/functools/collections/"
                "heapq/bisect/array/json/re/string/datetime/random，"
                "读写文件/网络/环境一律拒绝。用 print 输出，或只写一个表达式返回它的值；"
                "超过 2 秒终止。",
                (("code", "要执行的 Python 代码原文"),),
            ),
            "SOP2Prompt": (
                self.SOP2Prompt,
                "沉淀解决**某一类**问题的方法：凝练出这一类问题的名字，以及解决它要用的工具信息"
                "（接口/路径、参数、返回什么；探索到的环境知识也一并存）。"
                "沉淀过的条目从下一轮起每道题都会看得见 —— 下次遇到同一类问题照它做就行，"
                "不必重新探索一遍。"
                "它只沉淀、不产出命令、当回合也没有回执；同名覆盖旧条目、异名追加 ——"
                "所以照旧条目做而实测与它不一致时，用它把那条改成实测跑通的样子。"
                "`name` 是这一类问题的名字，要泛化到能覆盖以后的新任务"
                "（如「订去某地的机票的流程」，不要把某一次的目标写死进去），"
                "`sop` 是解决这类问题的做法与工具信息。"
                "它不影响你作答：答案照旧写在工具块外的 `<answer>` 里，"
                "两者写在同一条回复里即可。",
                (
                    ("name", "这一类问题的名字，泛化（如「订去某地的机票的流程」）"),
                    ("sop", "解决这类问题的做法与工具信息（接口、参数、返回）"),
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

        `path` 是枚举值，合法取值就是探明过的那几份（整条全路径、或它的文件名 —— 两种写法
        都列出来）⇒ 那张取值表跟着参数自己所在的那块走（不再单独占 system 的一段）。
        一份都没探明 ⇒ **不列**这个工具：它一个合法参数都没有，列出来只会换来一次"调用不成立"
        的空转；探明过就自动回来。调度那一侧不看这张表（`tool_call` 照旧认得它、给同一条不成立
        的结论），两处口径一致。
        """
        paths = cmd_explore.known_paths()
        if not paths:
            return {n: e for n, e in self._tools.items() if n != "readSandboxFile"}
        impl, desc, params = self._tools["readSandboxFile"]
        listed = "\n".join(
            f"- {path}（文件名 {cmd_explore.file_name(path)}）" for path in paths
        )
        tools = dict(self._tools)
        tools["readSandboxFile"] = (
            impl,
            f"{desc}\n已探明的文件（`path` 只能取这些）：\n{listed}",
            params,
        )
        return tools

    def hear(self, reply: str) -> None:
        """记下判题器 LLM 这回合的回复（`planner.task_channel` 每回合都调 —— 发命令/交答案
        那两轮没有 prompt，回复照样得进会话，否则回灌时它自己的命令凭空消失）。

        还没开过会话（这道题一次都没问过）⇒ 忽略。粘住的重复由 `Context.hear` 去重。任务
        回复里零星的 `<summary>` 一律当普通文字记 —— 摘要的唯一来源是 `adopt_summary`。
        """
        if self._context is not None:
            self._context.hear(reply)

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
        """
        entry = self._tools.get(tool_name)
        if entry is None:
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
            return ""  # params 不是 [(名, 文本)] 的形状
        if any(
            not isinstance(resolved.get(pname), str) or not resolved[pname].strip()
            for pname, _ in spec
        ):
            return ""
        return impl(**resolved)

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
