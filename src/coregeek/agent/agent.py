"""`Agent` —— 单实例的解题智能体：一个进程一个，跨回合活着；开拓者每次来用的都是它。

跨回合状态有**两处**，都在实例上：`_sop`（**整场**存活的流程表，第 37 步起是
`{流程名: 正文}`）与 `_context`（**任务内**的会话，第 25 步）。两条共同的账：

- **它坏了会怎样**：都只影响 prompt 的**内容**（答得好不好），**不碰红线** ——
  会话丢了 ⇒ 退化成单轮提问（第 25 步之前的行为）；流程表空着 ⇒ 那一段打占位。
- **不加锁**：判题器是**逐回合同步请求**；即便真有并发，GIL 下 `dict` 的赋值与读取不撕裂。
- **SOP 不按任务分区**：任务 A 沉淀的会灌进任务 B（用户拍板的取舍，记录、不修）。
  会话**按任务分区**（身份 = 题目原文），但**无上界增长**（先不压缩，同样记录、不修）。

`planner.task_channel` 每次都用包根那个 `AGENT`，而不是每次新建一个 —— 单实例是
"SOP 能跨回合长出来、会话能跨回合接上"的共同前提。
"""

from collections.abc import Callable

from .chat import is_prices_reply, is_summary_reply, strip_answers
from .context import Context
from .prompt import gen_compression_prompt, gen_news_prompt, gen_system_prompt
from .tools.cmd import executeCmd
from .tools.sop import store


class Agent:
    """会用工具解题的智能体。**建一个就够**（`coregeek.agent.AGENT`）。"""

    def __init__(self) -> None:
        #: 「沉淀的 SOP」—— 整场存活的跨回合状态之一，第 37 步起是**流程表**
        #: `{流程名: 正文}`（同名覆盖、异名追加、条数上限）。重启清空。
        self._sop: dict[str, str] = {}
        #: **价格期望与新闻指纹**（第 42 步）—— 跨回合状态之三。退化路径都想好了：
        #: 期望错了 = 采矿偏好偏一天，不碰红线；指纹丢了 = 重问一次新闻（额度 3/日）。
        self._news_digest = ""
        self._price_hints: dict[str, float] = {}
        #: **任务内**的会话上下文 —— 跨回合状态之二（第 25 步）。题目变了即换新；
        #: 任务结束不清（死会话，下场换题时自然被替）。
        self._context: Context | None = None
        #: 工具名 → (实现, 给 LLM 看的描述, **参数表** `((参数名, 用途), …)`)。
        #: 描述与参数说明都是 prompt 的一部分（由 `prompt.gen_all_tool_prompt` 生成），
        #: 措辞直接决定调用正确率；参数表同时是 `tool_call` 的**调度签名** —— 一张表
        #: 两处用，描述与调度不会分家。顺序即 prompt 里的顺序。
        #: ⚠️ **表必须由实例构造**：`SOP2Prompt` 写的是 `self._sop` ⇒ 只能是绑定方法。
        self._tools: dict[
            str, tuple[Callable[..., str], str, tuple[tuple[str, str], ...]]
        ] = {
            "executeCmd": (
                executeCmd,
                "在判题器的沙盒里执行一条命令（能跑基础 shell 与 python 指令，不能访问外网）。",
                (("cmd", "命令原文"),),
            ),
            "SOP2Prompt": (
                self.SOP2Prompt,
                "把一条解题流程沉淀进后续每一份 prompt 的「沉淀的SOP」段。"
                "它只沉淀、不产出命令、当回合也没有回执，但从下一轮起每道题都会看到它。"
                "`name` 是流程名（简短，如「找任务书」），`sop` 是做法总结——要任务无关、"
                "尽量简短；同名会覆盖旧流程、异名追加。"
                "它不影响你作答：答案照旧写在工具块外的 `<answer>` 里，"
                "两者写在同一条回复里即可。",
                (("name", "流程名，简短（如「找任务书」）"), ("sop", "该流程的做法总结")),
            ),
        }

    def chat(self, request: str, *, result: str = "", retry: str = "") -> str:
        """组装（**累积的**）会话发给判题器的 LLM。`request` = 题目原文。

        同一道题 ⇒ 续上之前的全部往来（`self._context`）；换题 ⇒ 新会话。每轮往里放什么：
        首问 = 题目（构造即问）；回灌轮 = 结果/纠错（`feed`）；无新内容的重问轮 =
        一句「请继续。」（`nudge`）—— 判题器的 LLM 是黑盒，会话停在它自己的输出上
        是个含糊指令。返回值是**标准 messages JSON**（第 28 步：`[{role, content}]`，
        自造文本版式实测效果非常差、已弃）。

        system（`prompt.py` 的段模板）**每次现刷**：SOP 是活的，任务进行中沉淀的下一轮
        就得看得见 —— 那是 `SOP2Prompt` "调用成功"的回执（它不产出命令）。
        """
        fresh = self._context is None or self._context.task != request
        if fresh:
            # 换题 ⇒ 新会话。粘住的回执也照样 feed：与旧的单轮行为一致（照样回灌）。
            self._context = Context(request)
        if result or retry:
            self._context.feed(result, retry)
        elif not fresh:
            self._context.nudge()
        self._context.system = gen_system_prompt(self._tools, self._sop)
        return self._context.render()

    def hear(self, reply: str) -> None:
        """记下判题器 LLM 这回合的回复（`planner.task_channel` 每回合都调 —— 发命令/
        交答案那两轮没有 prompt，回复照样得进会话，否则回灌时它自己的命令凭空消失）。

        还没开过会话（这道题一次都没问过）⇒ 忽略。粘住的重复由 `Context.hear` 去重。
        ⚠️ 第 41 步起**不再从这里提取摘要**（第 39 步的搭车协议作废）：任务回复里的
        零星 `<summary>` 一律当普通文字记 —— 摘要的唯一来源是 `adopt_summary`。
        """
        if self._context is not None:
            self._context.hear(reply)

    def adopt_summary(self, text: str) -> None:
        """把**压缩轮**的摘要记进会话上下文（第 41 步）。还没开过会话 ⇒ 忽略。

        调用方是 `task_channel` 的路由（裸 `<summary>` 回复 = 压缩请求的产物）：
        摘要进 `Context.summary`、回复**不进会话表** —— 它不是 LLM 在任务上说过的话。
        """
        if self._context is not None:
            self._context.summary = text

    def compression_request(self) -> str:
        """**压缩轮的 prompt**（第 41 步）：独立指令 + **原始上下文全文**。

        发送时机（第 43 步改定，用户拍板）：**回合最末尾的压缩闸门** ——
        `task_channel` 判据链算完 `prompt` 还是空（③ 命令轮 / ⑤ 答案轮）才发；
        判据 ②/④/⑥ 的模型请求永远优先（此前第 41 步只在命令轮捎带）。
        任务期间 prompt 不限量不计数。没开过会话 ⇒ `""`。
        原料由 `Context.material()` 给出：**原文永久保留**、压缩总从原文重来
        （用户拍板）；给任务 LLM 的才是压缩后的（摘要 + 窗口）。
        """
        if self._context is None:
            return ""
        return gen_compression_prompt(self._context.material())

    def news_question(self, news: str) -> str:
        """没任务时的**新闻查价** prompt（第 42 步）。**同一份 news 只问一次**（指纹
        去重 —— 任务线之外每游戏日只有 3 次额度）；news 空 / 指纹没变 ⇒ `""`。
        指纹在**发问时**就记下：判题器不答也只是"不再问了"，不重发同一份。
        """
        if not news:
            return ""
        digest = f"{len(news)}:{news[:64]}:{news[-32:]}"
        if digest == self._news_digest:
            return ""
        self._news_digest = digest
        return gen_news_prompt(news)

    def adopt_price_hints(self, hints: dict[str, str]) -> None:
        """新闻查价回复（裸 `<prices>` 块）→ 价格期望表（第 42 步）。

        粗粒度方向：up ×2 / down ×0.5 / flat ×1 —— 只修正挖矿性价比的**排序**，
        payload 的实时收购价每回合照读（期望是叠加项，不是替代）。
        """
        factor = {"up": 2.0, "down": 0.5, "flat": 1.0}
        self._price_hints = {kind: factor.get(d, 1.0) for kind, d in hints.items()}

    def price_hint(self, kind: str) -> float:
        """矿种的新闻期望系数。没问过新闻 ⇒ 1.0（无修正）。"""
        return self._price_hints.get(kind, 1.0)

    def tool_call(self, tool_name: str, params: list[tuple[str, str]]) -> str:
        """**顶层调度入口**：按名字调工具，返回要放进响应顶层 `executeCmd` 的那条命令。

        `params` 是 `tool_of` 解析出来的 `[(参数名, 原文), …]` —— 第 37 步起**只收具名
        参数**（严格解析产不出无名参数，位置填充机制随之删除）：**带名的按名对**，
        认不出的名字忽略（不为一个编造的名字作废整次调用）；声明了的参数**一个不少、
        值是非空字符串**才放行，最后 `impl(**resolved)` —— 无参数工具就是 `impl()`。

        ⚠️ **"返回值即命令"是一条铁律**：`""` = 这个工具不产出命令（`SOP2Prompt`）/
        调用不成立（未知工具、形状不对、缺参数、空白值），**下游不需要区分**，全落到"重问"。

        ⚠️ **绝不抛异常**（它跑在 `app.handle` 的 `try` 里，抛出去会把整回合所有角色的指令
        一起带走）⇒ 一切不成立都返回 `""`。⚠️ 边界校验只在**这一处**：这是唯一一个由
        外部字符串驱动的入口（推论：`SOP2Prompt` 走正常路径时永远不会收到空串 ——
        空白参数在进工具之前就被挡下，清不掉已存的流程）。
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
                # 认不出的参数名：忽略（宽容那一侧）—— 注意是"不进 resolved"，
                # 传给 impl 一样会 TypeError（它按声明收参）。
        except (TypeError, ValueError):
            return ""  # params 不是 [(名, 文本)] 的形状
        if any(
            not isinstance(resolved.get(pname), str) or not resolved[pname].strip()
            for pname, _ in spec
        ):
            return ""
        return impl(**resolved)

    def SOP2Prompt(self, name: str, sop: str) -> str:
        """把**一条**流程（`name` = 流程名、`sop` = 做法总结）沉淀进流程表。
        返回 `""` —— **它不产出命令**。

        存储规则（单条上限、条数上限、同名覆盖、截断留痕、内容没变就静默）在
        `tools/sop.py`，这里只管**把新表记在自己身上**。方法名同时是注册表里的工具名。

        ⚠️ **只做流程沉淀**（第 36 步的用户口径，第 37 步补上流程名）：**答案不归这个工具管**。
        同轮作答交给 `chat.answer_of`（答案写在工具块外的 `<answer>` 里）⇒ 沉淀与作答分家：
        这一回合算不算作答由那个谓词判，两条都走不通就落到判据 ⑥ 重问 —— 而沉淀**已经
        落库**了，丢的只是那一回合。
        ⚠️ **`sop` 里成对的 `<answer>…</answer>` 一定在入库前挖掉**（`chat.strip_answers`）：
        那段正文的用处正是讲"答案怎么写"，不挖掉就会带着这对串进后续每一份 prompt。
        **挖掉、不是作废整次调用** —— 沉淀是这个工具的全部价值，不该因为它多写一句示例
        就整段丢。**空文本 = 删掉那条**（只在直接调用时可达：`tool_call` 的闸门挡空白）。
        """
        sop, stripped = strip_answers(sop)
        self._sop = store(self._sop, name, sop, stripped=stripped)
        return ""

    @property
    def sop(self) -> dict[str, str]:
        """现在的流程表 —— 「沉淀的SOP」段的填充值。没沉淀过 ⇒ 空 dict。"""
        return self._sop

    def reset(self) -> None:
        """清空（流程表与会话**两处**）。**只给用例用** —— 单实例是模块级的，
        同一个测试进程里会跨用例串味。

        不复用 `SOP2Prompt("名", "")`：那个会打日志，而用例的 `assertLogs` 正盯着日志。
        """
        self._sop = {}
        self._news_digest = ""
        self._price_hints = {}
        self._context = None
