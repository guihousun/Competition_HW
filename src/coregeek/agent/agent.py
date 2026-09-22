"""`Agent` —— 单实例的解题智能体：一个进程一个，跨回合状态全在实例上。

两张流程表：`_sop`（正式，整场）、`_pre_sop`（暂存，任务内 —— 沉淀（`deposit`）先落这里，
判题器没报错的那一轮才由 `settle_deposit` 转正）。另有 `_news_digest`（新闻指纹，含"第几天"）、
`_context`（任务内会话，题目变了即换新）。状态丢了只影响 prompt 内容、不碰红线；判题器逐回合同步
请求 ⇒ 不加锁。`task.task_channel` 每次都用包根那个 `AGENT`。

⚠️ **新闻的产物不在这里**：查价的回复（裸 `<prices>`）由 `task.task_channel` 交给
`game.core.record_news` —— 那是一张**按天**的价格表，只被 game 侧的挖矿排序读。

`_answer` 与 `_submitted` 是短命的那两个：前者由 `submitAnswer` 写、`task_channel` 读、
`answer_task` 取走，**只活一回合**（每回合开头由 `task_channel` 清一次）—— 它是回合内的
一次交接，不是记忆；后者是"交过卷、判决还没到"的那笔账，由 `settle_deposit` 结（没报错 ⇒
转正、报 code 2 ⇒ 作废，都当场结掉）。
"""

import logging
from collections.abc import Callable

from . import cmd_explore
from .context import Context
from .prompt import (
    gen_compression_prompt,
    gen_news_prompt,
    gen_sop_request,
    gen_system_prompt,
)
from .tools import pyexec
from .tools.cmd import executeCmd
from .tools.sop import store

LOGGER = logging.getLogger(__name__)

#: 【工具调用】那行里每个参数值截到多少字（拍的；原文在任务行的「上一轮模型回复」里）。
ARGS_LOG_MAX = 1000

#: 压缩闸门：摘要与在途请求都没盖到的工具往来超过这个条数，才发一次压缩请求。
#: 用户口径「有大量上下文时（超过 5 次 tool）才压缩一次」—— 压早了那些原始往来当场
#: 被摘要替掉。调到很大即等于关掉压缩。
COMPRESS_AFTER_TOOLS = 5


def _call_text(params: list[tuple[str, str]]) -> str:
    """`[(参数名, 原文), …]` 打成一行的 `名=值`，没有参数打「无参数」；超限截断留痕。"""
    parts = []
    for name, value in params:
        value = str(value)
        if len(value) > ARGS_LOG_MAX:
            value = value[:ARGS_LOG_MAX] + f"…（共 {len(value)} 字）"
        parts.append(f"{name}={value}")
    return "，".join(parts) or "无参数"


def _collision_note(path: str, hits: list[str]) -> str:
    """文件名撞了时回给 LLM 的说明：只列撞上的那几份（"你指的是哪一份"），要它改写全路径。

    清单不在这里重抄 —— 它挂在 `readSandboxFile` 的描述里，与这条说明同处一份 prompt。"""
    return (
        f"{path} 对上了不止一份已探明的文件：{'、'.join(hits)}。"
        "`path` 请照抄完整路径 —— 只写文件名时它定不下是哪一份。"
    )


def _lookup_note(path: str) -> str:
    """本地没有这份时回给 LLM 的说明：把"已经派了一条查找命令"说出来，省得它再发一条。

    "我们还没摸过"与"沙盒里没有"在这里不必分开 —— 两种都走同一条查找命令。"""
    return (
        f"{path} 本地没有（清单里是已探明的那几份）⇒ 已让沙盒按文件名找它、找到就打印正文，"
        "结果下一回合随命令回执回来。这一份不用再自己发命令去找。"
    )


class Agent:
    """会用工具解题的智能体。建一个就够（`coregeek.agent.AGENT`）。"""

    def __init__(self) -> None:
        #: 沉淀的 SOP —— 流程表 `{流程名: 正文}`（同名覆盖、异名追加、条数上限）。
        self._sop: dict[str, str] = {}
        #: 本题暂存的沉淀（规则同 `_sop`）：`deposit` 先落这里，判题器没报错的那一轮才
        #: 转正；换题即作废 —— 没被验证过的经验不进「沉淀的SOP」段。见 `settle_deposit`。
        self._pre_sop: dict[str, str] = {}
        #: 交过卷、判决还没到的那笔账（`settle_deposit` 结：转正或作废都当场清）。
        self._submitted = False
        #: 新闻指纹。丢了只影响"白问一次新闻"，不碰红线。
        self._news_digest = ""
        #: 任务内的会话上下文。题目变了即换新；任务结束不清（死会话，下场换题自然被替）。
        self._context: Context | None = None
        #: 本回合交上来的答卷（`submitAnswer` 的参数值）—— 只活一回合，见模块 docstring。
        self._answer = ""
        #: 工具名 → (实现, 给 LLM 看的描述, 参数表)。描述与调度同源这一张表（描述由
        #: `prompt.gen_all_tool_prompt` 生成，不会分家）；顺序即 prompt 里的顺序。表必须由
        #: 实例构造：几个实现都是绑定方法（写的是会话与答卷）。
        self._tools: dict[
            str, tuple[Callable[..., str], str, tuple[tuple[str, str], ...]]
        ] = {
            "executeCmd": (
                executeCmd,
                """
在远程沙盒中执行基础 Shell/Python 命令，用于探索环境、查找/读取/修改文件、运行程序、调试和验证任务结果。
不能访问外网。

这是高成本工具，一次调用消耗 2 个回合。
能在一次命令中完成的连续操作应尽量合并；若后续操作依赖当前输出且无法安全合并，再拆分调用。
需要在多个候选值之间试错时，尽量在一次命令中批量尝试，并给每次尝试输出清晰标签 —— 回执是写给下一回合的自己读的。
任务书指定了脚本或验证方式时优先执行它，不要自行创造另一套验证方式。
未知文件、路径或环境信息应使用此工具探索。""",
                (("cmd", "需要在沙盒中执行的完整命令原文"),),
            ),
            "readSandboxFile": (
                self.read_sandbox_file,
                """
读取沙盒文件：`path` 取[路径清单]里的全路径或它的文件名 —— 清单里的本地就有，正文当回合到手、不花回合。
清单里没有的文件名也收：会去沙盒里按这个文件名找一次（多花一个回合），找到就给正文、找不到会明说。""",
                (("path", "文件的全路径、或它的文件名；清单外的名字会去沙盒里按文件名找一次"),),
            ),
            "python_exec": (
                self.python_exec,
                """
执行纯计算和数据处理，不访问沙盒、文件系统或网络。
适用于数学计算、字符串/JSON处理、数据转换、结果比较和命令构造。
比 executeCmd 省一个回合（它要 2 个回合、还要等沙盒回执）：不需要访问沙盒的活儿一律用它，不要占用昂贵的沙盒执行。""",
                (("code", "要执行的 Python 代码原文"),),
            ),
            "submitAnswer": (
                self.submitAnswer,
                """
提交任务的最终答案 —— 认定任务完成后用它交卷，答案写在 answer 参数里。
只放最终结果本身，不要带推导过程、解释或客套：判题器按答案里的字段完整度算通过率，
多写的字直接扣分。被判错时判题器会给出反馈，按反馈改了再交一次。""",
                (("answer", "要提交的最终答案原文"),),
            ),
        }

    def chat(self, request: str, *, result: str = "", retry: str = "") -> str:
        """组装累积的会话发给判题器的 LLM，返回标准 messages JSON。

        `request` = 题目原文；同一道题续上旧会话、换题换新。首问 = 题目；回灌轮 = 结果/纠错
        （`feed`）；无新内容的重问轮补一句「请继续。」。system 每次现刷：SOP 与沙盒清单
        是活的，下一轮就得看得见。渲染 = 题目 + 摘要 + **摘要没盖到的全部往来**。"""
        fresh = self._context is None or self._context.task != request
        if fresh:
            # 换题 ⇒ 上一道题没转正的沉淀作废：没被验证过的经验不跨题。"这个进程还没开过
            # 会话"不算换题（那是本题第一次开口，这道题刚沉淀的不能就此被自己清掉）。
            if self._context is not None:
                self._pre_sop = {}
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
        "沙盒里没有"。每行给两种写法（全路径、文件名）。调度侧不看这张表，口径一致。

        沉淀不在表里（第 144 步起它根本不是工具：交卷之后那一轮按 `SOP_REQUEST` 的格式
        回一个 `<sop>` 块），所以这张表就是注册表本身。"""
        tools = dict(self._tools)
        paths = cmd_explore.known_paths()
        if not paths:
            return tools
        impl, desc, params = self._tools["readSandboxFile"]
        # 构造成- 行的清单，给 LLM 看。每行两种写法：全路径、文件名。
        listed = "\n".join(f"- {p}，{p.split('/')[-1]}" for p in sorted(paths))
        tools["readSandboxFile"] = (
            impl,
            f"{desc}\n[路径清单](`path` 只能取这些值)：\n{listed}\n",
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
        """压缩轮的摘要记进会话上下文（裸 `<summary>` 回复的路由）；没开会话 ⇒ 忽略。

        摘要一到，`Context` 就把渲染起点推到上一次压缩请求的快照上 ⇒ 它盖住的那段不再逐条
        渲染（避免与摘要双份）。"""
        if self._context is not None:
            self._context.adopt_summary(text)

    def compression_request(self) -> str:
        """压缩轮的 prompt：独立指令 + 原始上下文全文（`Context.material`）。没开会话、
        或还没攒够上下文（未覆盖的工具往来 ≤ `COMPRESS_AFTER_TOOLS`）⇒ `""`。

        发送时机 = 回合末尾的压缩闸门（只剩命令轮）。这里顺手给上下文记一个快照
        （`sent_for_compression`）：摘要回来时按它划渲染起点 —— 判题器不答的话快照不动，
        那些往来照旧全量渲染，也照旧算在"还没攒够"那一侧。"""
        if self._context is None:
            return ""
        if self._context.uncompressed_tools() <= COMPRESS_AFTER_TOOLS:
            return ""
        self._context.sent_for_compression()
        return gen_compression_prompt(self._context.material())

    def sop_request(self) -> str:
        """沉淀轮的 prompt：沉淀指令（含 `<sop>` 输出定义）+ 现在的 SOP + 本题的完整记录。
        没开会话 ⇒ `""`。

        发送时机 = 交卷那一轮（判据链尾的沉淀闸门）：这一轮调了 `submitAnswer` 就发，
        不问回执 —— 等判决才开口会白占一个 LLM 回合。判题器在任务期间不计数，这次开口
        不吃额度。回复是裸 `<sop>` 块，下一轮随 `llmResp` 回来、由 `task.task_channel`
        解析后落暂存表 —— 解析压在"没任务"早返回之前，所以"答对了、任务已经结束"的那一轮
        也收得到。"""
        if self._context is None:
            return ""
        return gen_sop_request(self._sop, self._context.material())

    def read_sandbox_file(self, path: str) -> str:
        """读一份沙盒文件：本地探明过 ⇒ 正文当回合进会话、返 `""`（省一回合）；本地没有 ⇒
        返一条"按**文件名**去沙盒里找它并打印"的命令（正文下一回合随回执回来）；文件名对上
        不止一份 ⇒ 调用不成立，只回一条说明 —— 本地明明有，是 `path` 没写全。"""
        body = cmd_explore.body_of(path)
        if body:
            if self._context is not None:
                self._context.tool_output(body, f"【沙盒文件 {path} 的正文（本地已探明）】")
            LOGGER.info("【沙盒文件】：%s 本地取回 %d 字", path, len(body))
            return ""
        hits = cmd_explore.matches(path)
        if len(hits) > 1:
            LOGGER.info("【沙盒文件】：%s 这次调用不成立（对上 %d 条探明的路径）", path, len(hits))
            if self._context is not None:
                self._context.tool_output(
                    _collision_note(path, hits), "【沙盒文件：这次调用不成立】"
                )
            return ""
        # 本地没有这份 ⇒ 兜底：派一条按文件名查找的命令（它拼出来的目录不作数，照那个路径
        # `cat` 必然报错）
        LOGGER.info("【沙盒文件】：%s 本地没有 ⇒ 派一条按文件名查找的命令", path)
        if self._context is not None:
            self._context.tool_output(_lookup_note(path), "【沙盒文件：本地没有，去沙盒找】")
        return cmd_explore.lookup_command(path)

    def python_exec(self, code: str) -> str:
        """本地即时计算：产出当场记进会话、返回 `""`（不产命令）。比 `executeCmd` 省一整个
        沙盒往返。没开会话时产出丢弃（迟到的回复，无害）。"""
        output = pyexec.run(code)
        if self._context is not None:
            self._context.tool_output(output, "【本地 python 的执行结果（原文）】")
        return ""

    def news_question(self, news: str, day: int) -> str:
        """没任务时的新闻查价 prompt。同一天里同一份 news 只问一次（指纹去重，额度 3/日）；
        news 空 / 指纹没变 ⇒ `""`。指纹在发问时就记下：判题器不答也只是"不再问了"。

        ⚠️ **指纹要含"第几天"**：新闻每天发一次，而它说的"明天"是相对**发问那天**的 ——
        同一段文字第二天再现是新的一天（窗口该整体后移），照旧去重会让表永远停在第一天。"""
        if not news:
            return ""
        digest = f"{day}:{len(news)}:{news[:64]}:{news[-32:]}"
        if digest == self._news_digest:
            return ""
        self._news_digest = digest
        return gen_news_prompt(news)

    def _note_failed_call(self, why: str) -> str:
        """把"这次调用不成立"的说明回灌进会话（LLM 侧的唯一线索）。没开会话 ⇒ 只留日志。"""
        if self._context is not None:
            self._context.tool_output(
                f"这次工具调用没有发出去，也不会有它的执行结果。{why}。",
                "【工具调用：这次调用不成立】",
            )
        return ""

    def tool_call(self, tool_name: str, params: list[tuple[str, str]]) -> str:
        """**一条**调用的调度（一回合的入口是 `tool_calls`）：返回要放进响应顶层
        `executeCmd` 的那条命令。

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

    def tool_calls(self, calls: list[tuple[str, list[tuple[str, str]]]]) -> str:
        """顶层调度入口：一回合的调用们 ⇒ 要放进响应顶层 `executeCmd` 的那条命令。

        `calls` 是 `tool_of` 解出的整张表。**一轮只许一条**：并列 ⇒ **整轮不成立**，一条都不派、
        把原因回灌进会话（那一轮落重问）—— 派一半出去，"哪条跑了"日志上都答不出来。放行的
        走 `tool_call`（它一条不产命令就是 `""`，拼出来还是 `""`）。"""
        if len(calls) > 1:
            names = "、".join(name for name, _ in calls)
            LOGGER.info("【工具调用】：%s ⇒ 整轮不成立（一轮只调一个工具）", names)
            return self._note_failed_call(
                f"这一轮同时调了 {names} —— 一个回合只调一个工具，调用之后要等下一回合的"
                "回执。请把它们拆到不同的回合"
            )
        command = ""
        for tool_name, params in calls:
            command += self.tool_call(tool_name, params)
        return command

    def submitAnswer(self, answer: str) -> str:
        """注册表里的那只手：把 `answer` 记进 `self._answer`，`game.task.answer_task` 取走。

        恒返回 `""`：交卷不产出 `executeCmd`（真正发指令的是 `game.task.answer_task`，
        开拓者走 `actions.SubmitAnswer`）。它是答卷唯一的入口 —— "该提交什么"因此不必再
        从回复原文里解一遍，`tool_of` 认出什么就写进什么是同一个值。"""
        self._answer = answer
        return ""

    @property
    def answer(self) -> str:
        """本回合交上来的答卷；没调 `submitAnswer` ⇒ `""`。只读 —— 取走用 `take_answer`。

        `task.task_channel` 用它判"这一轮有没有答案"（判据 ③′/④/⑤ 与"交卷轮不压缩"）。"""
        return self._answer

    def take_answer(self) -> str:
        """取走答卷并清零 —— 交答案的调用点（`game.task.answer_task`）用它。

        取走即清是这条通道的记账方式；`task_channel` 每回合开头也调一次、把返回值丢掉：
        答卷是**回合内的一次交接**，上一回合没能交出去的那份就此作废（不然它会被当成
        下一回合的答案，甚至提交给另一道题）。"""
        answer, self._answer = self._answer, ""
        return answer

    def reject_shape(self) -> str:
        """整条回复像工具调用、但严格解析连工具名都取不出 ⇒ 记日志 + 把说明回灌进会话。
        调用方是 `task.task_channel`（那一轮落重问）。"""
        LOGGER.info("【工具调用】：形状没写对（取不出工具名）⇒ 这一轮落重问")
        return self._note_failed_call(
            "没能按【工具调用格式】从这条回复里解析出工具调用（取不出工具名）"
            "—— 工具名写在 <tool_name> 标签里，参数写在 <tool_param> 里、"
            "每个参数各用一对标签包裹（如 <cmd>命令</cmd>）"
        )

    def deposit(self, name: str, body: str) -> None:
        """沉淀一条条目（`name` = 这类问题的名字、`body` = 四栏正文）—— 落**暂存表**。

        第 144 步起这条通道不是工具：回复里裸写 `<sop>` 块，`task.task_channel` 用
        `chat.sops_of` 解出来调这里。`settle_deposit` 转正之后才进「沉淀的SOP」段；
        存储规则（同名覆盖、条数上限、截断留痕）在 `tools/sop.py`。空文本 = 删掉那条。"""
        self._pre_sop = store(self._pre_sop, name, body, where="暂存")

    def settle_deposit(self, submitted: bool, accepted: bool) -> None:
        """结沉淀的账（`task.task_channel` 每回合调一次，压在 `<sop>` 落库与工具调度之后）。

        `submitted` = 这一轮调了 `submitAnswer`；`accepted` = 这一轮判题器**没报 `code 2`**
        （"答案不对"那个码，由调用方判 —— 码表的含义不写进本模块）。**这笔账交过卷就挂着**，
        直到判决到齐：没报错、暂存表里又有东西 ⇒ 整批转正 —— 判题器只在出错时吭声，"没报错"
        就是它的成功回执（没有更硬的判据，未实测）；报 `code 2` ⇒ 判过了、没过，账结掉
        （那几条留在暂存表里，下次交卷判过时照样转正；换题则整表作废）。
        ⚠️ 转正看的是**暂存表非空**：沉淀的回复可能比判决晚好几轮才回来（交卷轮发的请求，
        回复掉进了后面的某一轮），盯着"交卷的下一轮"会把它整条错过。
        这两个位置都是必需的：压在落库之后（`<sop>` 就是这一轮才进暂存表的），又压
        "没任务"早返回之前（答对了的那一轮题目已经空了，转正照样得发生）。"""
        if self._submitted:
            if not accepted:
                self._submitted = False
            elif self._pre_sop:
                self._promote()
                self._submitted = False
        if submitted:
            self._submitted = True

    def _promote(self) -> None:
        """暂存表整批并进正式表（同名覆盖），并清空暂存 —— 每条的日志由 `store` 打（带落点）。"""
        for name, text in self._pre_sop.items():
            self._sop = store(self._sop, name, text, where="正式")
        self._pre_sop = {}

    @property
    def sop(self) -> dict[str, str]:
        """正式流程表 —— 「沉淀的SOP」段的填充值。没沉淀过 ⇒ 空 dict。"""
        return self._sop

    @property
    def pre_sop(self) -> dict[str, str]:
        """暂存流程表 —— 已沉淀、还没转正的那些（不进 prompt）。"""
        return self._pre_sop

    def reset(self) -> None:
        """清空全部跨回合状态（两张流程表、新闻指纹、会话、答卷）。只给用例用 ——
        单实例是模块级的，会跨用例串味。⚠️ 价格表在 `game.core`（模块级），不在本实例上；
        用例隔离由 `tests._fixtures._reset_ledgers` 一并清。"""
        self._sop = {}
        self._pre_sop = {}
        self._submitted = False
        self._news_digest = ""
        self._context = None
        self._answer = ""
