"""`Agent` —— 单实例的解题智能体：一个进程一个，跨回合活着，跨回合状态全在实例上。

`_sop`（流程表，整场存活）、`_news_digest`/`_price_hints`（新闻指纹与价格期望）、`_context`
（任务内会话，题目变了即换新）。失败退化路径：状态丢了只影响 prompt 的内容、不碰红线
（会话丢了 ⇒ 退化成单轮提问）；判题器逐回合同步请求 ⇒ 不加锁。SOP 不按任务分区、会话按
任务分区（身份 = 题目原文）。`planner.task_channel` 每次都用包根那个 `AGENT`。
"""

from collections.abc import Callable

from .chat import is_prices_reply, is_summary_reply, strip_answers
from .context import Context
from .prompt import gen_compression_prompt, gen_news_prompt, gen_system_prompt
from .tools import pyexec
from .tools.cmd import executeCmd
from .tools.sop import store


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
                "在判题器的沙盒里执行一条命令（能跑基础 shell 与 python 指令，不能访问外网）。",
                (("cmd", "命令原文"),),
            ),
            "python_exec": (
                self.python_exec,
                "在本地即时执行一段**纯计算**的 Python：结果当回合就回到你面前"
                "（不走判题器沙盒、没有 15 秒限制，但**看不见沙盒里的任务文件**——"
                "读任务文件还是用 executeCmd）。只允许计算：import 仅限 "
                "math/cmath/decimal/fractions/statistics/itertools/functools/collections/"
                "heapq/bisect/array/json/re/string/datetime/random，"
                "读写文件/网络/环境一律拒绝。用 print 输出，或只写一个表达式返回它的值；"
                "超过 2 秒终止。",
                (("code", "要执行的 Python 代码原文"),),
            ),
            "SOP2Prompt": (
                self.SOP2Prompt,
                "把一条解题流程或探索到的**环境知识**沉淀进后续每一份 prompt 的「沉淀的SOP」段"
                "（接口描述、文件路径、格式约定这类事实也要存）。"
                "它只沉淀、不产出命令、当回合也没有回执，但从下一轮起每道题都会看到它。"
                "`name` 是条目名（简短；流程如「找任务书」、知识如「接口-查询」），"
                "`sop` 是做法总结或知识本身——要任务无关、尽量简短；"
                "同名会覆盖旧条目、异名追加。"
                "它不影响你作答：答案照旧写在工具块外的 `<answer>` 里，"
                "两者写在同一条回复里即可。",
                (("name", "条目名，简短（流程如「找任务书」、知识如「接口-查询」）"), ("sop", "做法总结或知识本身")),
            ),
        }

    def chat(self, request: str, *, result: str = "", retry: str = "") -> str:
        """组装累积的会话发给判题器的 LLM。`request` = 题目原文；同一道题续上旧会话、换题
        换新。首问 = 题目（构造即问）；回灌轮 = 结果/纠错（`feed`）；无新内容的重问轮 = 一句
        「请继续。」（`nudge`）—— 判题器的 LLM 是黑盒，会话停在它自己的输出上是个含糊指令。
        返回值是标准 messages JSON（`[{role, content}]`）。

        system（`prompt.py` 的段模板）每次现刷：SOP 是活的，任务进行中沉淀的下一轮就得看得见
        —— 那是 `SOP2Prompt` "调用成功"的回执（它不产出命令）。
        """
        fresh = self._context is None or self._context.task != request
        if fresh:
            # 换题 ⇒ 新会话。粘住的回执也照样 feed（照样回灌）。
            self._context = Context(request)
        if result or retry:
            self._context.feed(result, retry)
        elif not fresh:
            self._context.nudge()
        self._context.system = gen_system_prompt(self._tools, self._sop)
        return self._context.render()

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
            self._context.tool_output(output)
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
        """把一条条目（`name` = 条目名、`sop` = 流程总结或环境知识）沉淀进流程表，返回 `""`。

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
