"""决策入口：`Turn` → `roleCommandMap`。**策略只写在这里。**

角色每回合按 `docs/策略指导.md` 的优先级**择一**（每回合每个角色只能一个动作，任务书 L177）。
三条线 —— **白天开拓者去接任务、白天工人建造、夜里（还没被钉住的）角色上炮位**：

**白天**（开拓者，见 `_take_task`）：

1. **去任务点接任务** —— 走到最近一个**能接**的任务点旁边，贴着就 `acceptTask`
   （任务书 §4.6.2：「开拓者处于任一格即可领取任务」，而任务点挡路 ⇒ `step_toward`
   天然停在"周围一格内"，与 `build` / `collect` 是同一条契约）。一个能接的都没有 ⇒
   **原地待命**（空指令合法且不计异常）。

**白天**（工人）：

2. **建武器** —— 武器数 < 角色数、金币够。落点见 `grid.weapon_sites`：
   **后列 1 格 + 前排两角**（用户选定，这样人站在落点上就能同时够着炮和基地，
   升级/维修券才用得上），种类与落点按 `WEAPONS_BY_SITE` 一一对应（按射程配）。
3. **采石砌墙** —— "工人最先建立武器，然后找石矿建墙，找石矿应该要去**最近的**，
   另外**必须在晚上到来前将墙建好，注意计算回合数**"。攒几块不写死，按**白天还剩的回合数**
   现算（见 `_stones_to_mine`）。墙砌在**面向机器人进攻的方向**（保护基地），背面留缺口。
   **墙砌完之后不再闲置**：转去采**收购价最高**的矿（见 `_mine_spare_ore` / `_pick_ore`）
   —— 「手里面保持能建造墙的石头量就行，**然后**选择价格最高的矿」。顺序不能反：
   墙只吃石头，先挑最贵的铜就永远砌不成墙。价目取自载荷 `vendorShopList`（`Turn.vendor_prices`），
   **不写死"铜 > 铁 > 石头"**（那只是样例的价目，官方消息会让它波动）。

**夜里**（还没被任务钉住的角色 —— §4.4 里 `attack` 的可用角色列写的就是"全部"）：

4. **回炮位操炮** —— 走到最近的一座还没被本回合别人认领的武器旁，贴着就开火（见 `_defend`）；
   够不着/没敌人/炮在冷却就**站在炮位待命，什么都不发**（空指令合法且不计异常）。
   火力目标 = **射程内血最少的机器人**（补刀）。

> ⚠️ **开拓者一旦领到任务就被钉死**：离开己方任务点周围一格内任务立即作废（任务书 L379），
> 所以它**连夜里都不回炮位**（代价：那三座炮少一个人操）。分支写在 `plan()` 循环的**最前面**
> —— 它是全局最早的判据（任务在不在，与昼夜无关），放在那里昼夜两条分支才各自干净。

返回 `{角色ID: 指令}`，key 用**字符串** —— JSON 对象的 key 本来就是字符串。
⚠️ **`attack` 那条是唯一的例外**：它的 key 是**武器 id**，操控者在 `controllerId` 里。

任务线的另一半不在这个返回值里：`task_channel(turn)` 单独产出响应顶层的
**`(prompt, executeCmd)`** —— 与判题器的 LLM、与它的沙盒打交道的**唯一**通道。
它**没有状态、也没有第二个使用者**，所以留在本模块当一个纯函数，不另开一个模块、
也不改 `plan()` 的签名。

那条通道是一条**工具调用回路**（`<tool>…</tool>` 是我们与 LLM 约定的协议）：

    LLM 只回一条命令 → 我们把它放进响应顶层的 `executeCmd` → 判题器本回合在沙盒里跑
    （接口文档 L210）→ 下一回合 `lastCmdResult` 带回执行结果 → 我们把它**原文**回灌给 LLM
    → ……直到 LLM 不再要命令、直接给答案 → `_answer_task` 每回合 `submitAnswer`。

判题器答错了（`errors` 里的 `errorCode 2`）就把"上次答错了、上次交的是什么"一起带回去重问。
**回路的前半截在第 16 步之前根本不存在**：`executeCmd` 恒为 `""`，LLM 只能凭空猜答案 ——
那正是"任务一直失败"的根因。详见 `task_channel` 的判据表。
"""

import logging
from collections.abc import Iterator, Mapping, Set
from typing import Any

from ..protocol import actions  # 唯一一条"由内往外"的依赖：指令只能经 Action 产出
from .grid import Pos, step_toward, wall_cells, weapon_sites
from .map import STONE
from .roles import BaseRole, Pioneer, Worker
from .world import Turn, Weapon

LOGGER = logging.getLogger(__name__)

#: 三座武器的**种类，下标与 `grid.weapon_sites()` 的落点一一对应**（用户选定）：
#: 后列 1 格 → 火箭；前排两角 → 加特林、电磁炮。
#: **按射程配** —— 后列被基地挡住、离机器人最远，射程在那里几乎用不上，给最长的火箭最不吃亏
#: （样例 4/7/∞ 与任务书表格 3/6/10 **两套互相矛盾的数据下都是这个结论**）；
#: 加特林与电磁炮去主战线上的两个前角。
#: 数量上限 = 角色数，正好 3 —— 与策略指导"武器只有建立三个才有意义，建立多了没有意义"一致。
#: 任务书 §4.5.1 表格写"每种 ≤3"（合计 9 座）、补充说明写"**全局同时最多 3 座**"，
#: **原文自相矛盾**，取保守的那一个。
WEAPONS_BY_SITE = ("rocket", "gatling", "railgun")

#: 建一座武器的金币（任务书 §4.5.1，三种同价）。
WEAPON_COST = 25

#: 围墙的 `name` 与代价。代价是**石头×1**，从**建造者自己的背包**扣（不是全队共享）；
#: 拆了不返还（任务书 L209）。
WALL = "wall"
WALL_COST = 1

#: 一块石头的完整代价：1 回合采集 + 1 回合挪到下一格墙边 + 1 回合建造。
ROUNDS_PER_STONE = 3

#: 留给"从矿走回工地、把石头砌完"的容错余量（用户指定 5 回合）。
#: 它同时吸收**距离估算的误差** —— 下面的距离一律用切比雪夫，绕障时会低估。
TIME_MARGIN = 5

#: 任务提问模板（发给判题器的 LLM，回复从下一回合 payload 的 `llmResp` 回来）。
#: 三段：**怎么要命令**（`<tool>` 工具协议）、**什么时候直接作答**、**题目本身**。
#: `{result}` / `{retry}` 两段由 `_asked` 现拼，没有就是空串 —— 六条判据共用一个模板。
#:
#: **本步最大的猜测** —— `<tool>` 这个形状文档一个字没规定（任务书只写了沙盒"能跑基础的
#: shell 指令与 python 指令"），是我们自己定的协议，只能靠"判题器的 LLM 认不认"来检验。
#: 同理**不硬塞答案格式**：任务原文里既然带着"三方 API 文档"，题目自己很可能就规定了
#: 答案形式（通过率按"字段个数"算 ⇒ 是多字段结构化答案），我们猜错反而把 LLM 带偏。
TASK_PROMPT = (
    "你在一个沙盒环境里完成下面这道任务题。沙盒中能执行基础的 shell 指令与 python 指令。\n"
    "需要先取信息时，只回复一条要执行的命令，用 <tool> 与 </tool> 包起来，例如：\n"
    '<tool>python -c "print(1+1)"</tool>\n'
    "我下一回合把沙盒的执行结果原文发给你。信息够了就直接给答案：\n"
    "只写答案本身，按题目要求的形式作答，不要解释、不要前言、不要 Markdown 代码块标记。\n"
    "{result}{retry}\n题目：\n{task}"
)


def plan(turn: Turn) -> dict[str, dict[str, Any]]:
    cmds: dict[str, dict[str, Any]] = {}
    #: 本回合已被认领的落脚格。不设它，两个人冲同一个空格会按"目标点争夺"双双停住（任务书 §4.5.4）
    claimed: set[Pos] = set()
    #: 已被认领的**建造格**（武器与围墙共用）。与 `claimed` 分开：建造格是"要往里投料的位置"
    sites: set[Pos] = set()
    #: 已被认领的**武器**（夜里一人只能操一座）
    taken: set[Pos] = set()
    budget = turn.gold
    slots = _slots(turn)

    for role in turn.roles:
        # **服任务中的开拓者：钉死。** 离开己方任务点周围一格内任务立即作废（任务书 L379），
        # 所以它**连夜里都不回炮位**。判据用**载荷事实**（`phase_task`），不是自己记的
        # "谁领了任务" —— 重放 / 换回合 / 崩溃都不会让这一条错。
        # 放在循环最前面是有意的：它是全局最早的判据（任务在不在，与昼夜无关），
        # 摆在这里昼夜两条分支才各自干净，而不用在两边各写一遍。
        if isinstance(role, Pioneer) and turn.phase_task:
            _answer_task(role, turn, cmds)
            continue

        if not turn.is_day:
            # 夜里**还没被钉住**的角色回炮位（§4.4 的 attack 可用角色就是"全部"）
            _defend(role, turn, cmds, claimed, taken)
            continue

        if isinstance(role, Pioneer):
            _take_task(role, turn, cmds, claimed)  # ← 第 10 步之前这里是一句 continue
            continue

        if not isinstance(role, Worker):
            continue  # 既不是开拓者也不是工人 ⇒ 不是可操控单位（`roles.make` 已挡过一道）

        slot = next(slots, None)
        if slot is not None and budget >= WEAPON_COST:
            kind, cell = slot
            budget -= WEAPON_COST  # **认领即预留**：宁可这回合少建，不可超支
            sites.add(cell)
            if role.pos.dist(cell) <= 1:
                # 「**站位即建造位**」：`build` 要求目标在自身一格内，而 `step_toward`
                # 恰好把工人停在贴着目标的那一格 —— 两条规则正好对上，不用先挪开再建。
                if _emit(cmds, role, actions.Build, kind, cell):
                    continue
            else:
                # 把 `sites` 也当障碍：免得工人径直走到建造格**上**去（那样建完自己站在武器里）
                walk = turn.map.blocked | claimed | sites
                step = step_toward(role.pos, cell, walk, turn.map.size)
                if step is not None and _emit(cmds, role, actions.Move, step):
                    claimed.add(step)
                    continue
            # 建不了 / 走不到 → 落到下面，别杵着

        _build_walls(role, turn, cmds, claimed, sites)

    return cmds


#: 工具调用的定界符。**这是我们自己定的协议**（任务书只说了沙盒里能跑 shell 与 python，
#: 没说 LLM 该怎么要一条命令），所以 `_is_tool_reply` 判得**宽**：只要出现开标签就算
#: "它想跑命令"，格式凑不齐就去重问，而不是当成答案交上去。
_TOOL_OPEN = "<tool"
_TOOL_CLOSE = "</tool>"


def task_channel(turn: Turn) -> tuple[str, str]:
    """本回合任务的 `(prompt, executeCmd)` —— **响应顶层那两个字段的唯一来源**。

    **为什么合成一个函数**（而不是 `prompt_for` + `execute_for`）：这两条输出共用同一串
    判据，而那条链的核心不变量正是"**两者互斥**"。拆开就是把同一条链写两遍，任何一次
    单边修改都会造成"同一轮既提问又发命令"（LLM 在没看到结果的情况下作答 ⇒ 又要一遍
    同一条命令 ⇒ 活锁）或"两边都不发"（见判据 5 的畸形回复）。两份真相迟早对不上。

    **纯函数、无状态** —— 这是本模块最重要的架构不变量。任务线要靠跨回合标志位才能跑的话，
    那个位一旦卡住就**静默关掉整条任务线**（第 11 步为这条否决过"只交一次"的优化），
    而且出问题时日志上什么异常都看不出来。

    判据**从上往下，先命中先返回**：

    1. **没任务，或名册里没有开拓者 ⇒ 什么都不发。** 沙盒"仅任务期间可用"
       （接口文档 L208），任务外一次都不碰。
       ⚠️ **"开拓者还活着"这半条是显式补的**：`submitAnswer` 走 `roleCommandMap`，
       而死了的人根本不在 `model._character` 给出的 `roles` 里 ⇒ `_answer_task` **天生**进不来；
       但 `executeCmd` 是**响应顶层字段**，不经过角色循环、也不经过 `Action` 的权限闸门
       ⇒ 那条白送的闸门对它**完全不存在**。同一件事在一条通道上有闸门、在另一条上没有，
       迟早出事。人死了任务不再计分，停在这里；名册被畸形载荷清空时也是"少做"（一贯方向）。
    2. **沙盒刚交作业（`cmd_result` 非空）⇒ 回灌结果、这轮绝不发命令。**
       **必须压在判据 3 前面**：接口文档 L33 给 `lastCmdResult` 专门写了一句
       "未发命令时为空字符串"，而 L31 的 `llmResp` **一个字没写** —— 文档作者对前者明说了
       "不粘"，对后者没说。所以 `llm_resp` 必须按**可能粘住**设计：万一它还停在上轮那条
       `<tool>…</tool>` 上，判据 2 先命中就避免了同一条命令被反复丢进沙盒。
       附带挡住"沙盒结果万一延迟两回合" ⇒ 同一命令发两遍。
    3. **回复里有 `<tool>…</tool>` ⇒ 把那条命令交给判题器**（不提问）。任务存续期间 LLM
       不限量也不计数（接口文档 L198），所以这里不心疼额度，心疼的是"让它拿着过期结果作答"。
    4. **判题器说答案错了 ⇒ 带着"上次答错了"重问。** 见 `_retry_note` 的触发条件。
       排在 3 之后：`code 2` 会连着报几轮（我们每回合重交旧答案），而 LLM 若在这时回了新的
       工具调用，让它先跑（那是进展），纠错下一轮还在。
    5. **回复是最终答案 ⇒ 什么都不发**，开拓者那边每回合在 `submitAnswer`。
       判据用 `_is_tool_reply` 而**不是**"取不出命令"：`<tool` 有开无闭时 `_tool_command`
       取不到东西，若按"取不到 = 是答案"处理就会**把半条工具调用当答案交上去**，
       而 `_answer_task` 又跳过工具回复 ⇒ **两条通道同时哑火，永久空转**
       （`llm_resp` 粘住的话下一回合还是同一条畸形回复，日志上还什么都看不出来）。
       所以畸形的落到判据 6 去重问，而不是到这里。
    6. **否则（第一次提问 / 畸形回复重问）⇒ 只把题目发出去问。**
    """
    if not turn.phase_task or not any(isinstance(r, Pioneer) for r in turn.roles):
        return "", ""

    reply = turn.llm_resp.strip()
    command = _tool_command(reply)
    retry = _retry_note(turn)

    if turn.cmd_result:
        return _asked(turn, result=turn.cmd_result, retry=retry), ""
    if command:
        return "", command
    if retry:
        return _asked(turn, result="", retry=retry), ""
    if reply and not _is_tool_reply(reply):
        return "", ""
    return _asked(turn, result="", retry=""), ""


def _asked(turn: Turn, *, result: str, retry: str) -> str:
    """按 `TASK_PROMPT` 拼一条 prompt。`result` / `retry` **没有就不占地方**。

    两段各自带标题：沙盒输出是**任意文本**（可能是 JSON、可能是报错、可能带换行），
    没有标题档着，LLM 分不清哪一段是题目、哪一段是它要的输出。
    """
    return TASK_PROMPT.format(
        result=f"\n【上一条命令的执行结果（原文）】\n{result}\n" if result else "",
        retry=(
            f"\n【你上一次提交的答案被判定为不正确】\n{retry}\n请重新作答。\n"
            if retry
            else ""
        ),
        task=turn.phase_task,
    )


def _is_tool_reply(reply: str) -> bool:
    """这条回复是**工具调用**吗？判据只认开标签出现。

    判得比分隔符宽是有意的：`<tool ls`、`<tool\n>` 这类**想跑命令但格式没凑对**的回复
    也该被认出来 —— 落到"重问"而不是"当答案交上去"。代价是答案里若含字面量 `<tool`
    会被误判（拒绝提交、改问），概率极低，而且降级方向是"少交一次"不是"发非法指令"。

    **`task_channel` 判据 5 与 `_answer_task` 必须用同一个谓词** ——
    一边当命令、一边当答案就是第二份真相。
    """
    return _TOOL_OPEN in reply


def _tool_command(reply: str) -> str:
    """从回复里取出**第一条**要执行的命令；取不到（没有标签 / 有开无闭 / 内容是空的）⇒ `""`。

    只取一条：`executeCmd` 只有一个字段、判题器一回合只跑一条（接口文档 L210），
    多要几条也只会丢掉。开标签按 `>` 定位而不是硬写 `<tool>`，这样 `<tool >` 也认。
    """
    start = reply.find(_TOOL_OPEN)
    if start < 0:
        return ""
    head_end = reply.find(">", start)
    tail = reply.find(_TOOL_CLOSE, start)
    # 开标签没闭合、或 `</tool>` 跑到 `>` 前面（`</tool>` 单独出现）⇒ 这条回复是坏的
    if head_end < 0 or tail < head_end:
        return ""
    return reply[head_end + 1 : tail].strip()


def _retry_note(turn: Turn) -> str:
    """该往回带"你上次答错了"吗？该 ⇒ 返回上次交的答案原文，否则 `""`。

    三个条件缺一不可：

    - **`errors` 里有 `code == 2`**（答案不正确，接口文档 L181-198）。只认这一个码 ——
      1（任务超时）与 5（LLM 额度超限）是终局，3/4 重问也救不回来。**码的含义不写进代码**，
      那是会跟接口文档漂移的第二份真相（`CLAUDE.md` 已有这条规矩）。
    - **回复不是工具调用**：否则会往 prompt 里塞"你上一次的答案是 `<tool>ls -la</tool>`
      被判错了"这种胡话。工具回复分两路都要挡：**取不出命令**的（`<tool ls`）会落到
      判据 4 去用这个值，**取得出命令**的（`<tool>ls</tool>`）在判据 3 就走了、
      但它与 `cmd_result` 同时出现时（判据 2）照样会被带上。
    - **回复非空**这一条**不需要单独写**：空回复时本函数返回 `""`，而 `_asked` 里
      那两段是"空就不占地方"的。任务刚换时（上一条超时结束、开拓者立刻接了新任务）
      `errors` 里那个 2 是**旧账**，`llm_resp` 往往是空的 —— **没有答案可骂就不骂**，
      这件事由"返回空串"天然表达，别再加一句 `if reply`。
    """
    if not any(e.code == 2 for e in turn.errors):
        return ""
    reply = turn.llm_resp.strip()
    return reply if not _is_tool_reply(reply) else ""


def _slots(turn: Turn) -> Iterator[tuple[str, Pos]]:
    """本回合可以开建的 `(武器类别, 落点)`，按优先级排；没名额就一个都不产出。

    三道门槛，缺一座都不该建：

    - **白天** —— `build` 仅白天可用（任务书 §4.4）。夜里 `build` 会被判无效。
    - **份额** = `len(roles)` —— 用户规则"武器数量 < 总角色数"（3 角色 ⇒ 3 座）。
    - **落点是空的** —— 建在已有武器上会把它**覆盖**成 level1（§4.5.1 补充说明），
      25 金币打水漂还降级；建在墙/矿上也必然失败。

    基地没了（`station is None`）就没有可建造区 ⇒ 不建。**这是故意的降级方向。**
    """
    if not turn.is_day:
        return
    station = turn.map.station
    if station is None:
        return

    have = {w.kind for w in turn.weapons}  # 名册在 `Turn.weapons`（网格里那份第 10 步删了）
    need = len(turn.roles) - len(turn.weapons)
    if need <= 0:
        return

    blocked = turn.map.blocked
    #: **先按种类配对、再滤**。反过来（先滤种类、再 `zip` 落点）的话，落点会整体前移 ——
    #: 后列那格被占时"少一座火箭"会把加特林塞进给火箭留的格子上，而**落点与种类是绑死的**
    #: （`WEAPONS_BY_SITE` 与 `weapon_sites` 下标一一对应）。
    yield from [
        (kind, cell)
        for kind, cell in zip(WEAPONS_BY_SITE, weapon_sites(station, turn.map.size[0]))
        if kind not in have and cell not in blocked
    ][:need]


# ── 开拓者的任务线 ──────────────────────────────────────────────────
def _take_task(
    role: BaseRole, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos]
) -> None:
    """白天：走到最近一个**能接**的任务点旁边，贴着就 `acceptTask`。

    与 `build` / `collect` **同一条契约**：任务点挡路（任务书 L85），`step_toward`
    天然停在贴着它的那一格，而那正是"周围一格内"（§4.6.2）—— 不用先挪开再领。

    只认**锚点格**就够：走到锚点旁边必然满足"任一格周围一格内"（锚点本身就是其中一格；
    即便 `taskPosition` 报的是任务点2 的另一格，走到它旁边照样合法）。

    一个能接的点都没有（都在刷新冷却里 / 已做完）⇒ **什么都不发**。**不去冷却中的点蹲守**：
    白天走过去、夜里回炮位、第二天再走过去 —— 第 8 步 `_ring` 那个"两格之间对着改目标
    转到天黑"是同一类坑。

    ⚠️ 领到任务之后本函数就再也进不来了：`plan()` 最前面那道 `turn.phase_task` 分支
    会先一步接管（钉死）。
    """
    if not turn.task_points:
        return
    # "已经贴着就领"与"走过去"分两步判：合成一步（先滤掉 ≤1 的点、再取最近）会在
    # 站在两个任务点中间时**舍近求远**。
    if min(role.pos.dist(p) for p in turn.task_points) <= 1:
        _emit(cmds, role, actions.AcceptTask)
        return
    # 并列按坐标排：与 `_defend` 的 `(dist, id)` 同一条理由 —— 先后不能取决于
    # payload 里的顺序，否则用例复现不了。
    target = min(turn.task_points, key=lambda p: (role.pos.dist(p), p))
    _step(role, target, turn, cmds, claimed)


def _answer_task(role: BaseRole, turn: Turn, cmds: dict[str, dict[str, Any]]) -> None:
    """服任务中：把手上的答案**原样**交上去。**这里从来不移动** —— 一旦挪出任务点
    周围一格，任务立即作废（任务书 L379），这一趟就白钉了。

    答案 = `llmResp` 原文（只去首尾空白），不做任何加工：我们不知道判题器要什么格式，
    唯一能做的是原文进、原文出。**空答案不发** —— 那可能被判成"字段缺失"，
    而那正是红线里的"指令非法"。闸门只管"谁"，"空不空"归这里把关（与 `build` 的昼夜门同一条做法）。

    **每回合都交**：接口文档 L140「以之前提交过的**通过率最高的**答案计算积分与金币」
    —— 反复提交是判题器**预期的**用法。重复交同一个答案分数不变，而开拓者被钉在这里、
    本来也没有第二个动作可做。⚠️ **这就是本步不需要任何跨回合状态的全部理由**，
    别把它"优化"成"只交一次"：那要记住交没交过，而卡住的状态会**静默关掉整条任务线**。

    ⚠️ **工具调用绝不能当答案交上去**（`_is_tool_reply`）：那条回复要的是"去沙盒跑一下"，
    交上去等于把 `<tool>ls</tool>` 当成答案。用的谓词与 `task_channel` 判据 5 **同一个**
    —— 一边当命令、一边当答案就是第二份真相。
    """
    answer = turn.llm_resp.strip()
    if answer and not _is_tool_reply(answer):
        _emit(cmds, role, actions.SubmitAnswer, answer)


# ── 白天：采石 → 砌墙 ────────────────────────────────────────────────
def _build_walls(
    role: Worker, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos], sites: set[Pos]
) -> None:
    """白天：先攒石头，再把墙砌到优先级最高的那一格上；**砌完了就去采最值钱的矿**。

    每回合独立判定，**不存任何跨回合状态** —— 所以矿采没了、墙被别人砌了、
    白天快过完了，下一回合都能自动跟着变。

    用户策略指导那条的后半句在这里兑现（「手里面保持能建造墙的石头量就行，
    然后选择价格最高的矿」）：
    **"保持够砌墙的量"这一半本来就有** —— `_stones_to_mine` 拿"还差几格墙"当上限；
    补上的是"然后"：16 格砌完之后工人原本**原地闲置**，现在转去采矿。
    """
    free = [c for c in _ring(turn) if c not in sites]
    if not free:
        # 墙砌满了。手里剩的石头留给第二天补墙（**没有转移物品的指令**，给不了别人），
        # 人不再闲着 —— 白天不采，夜里就只是站在炮位上。
        _mine_spare_ore(role, turn, cmds, claimed, sites)
        return
    target = free[0]  # `_ring` 已按建造优先级排好
    #: **只认石矿**：墙只吃石头（`build` 围墙要求背包里有石头），铁/铜再多也砌不了墙。
    mine = _pick_ore(role.pos, turn.map.ores, turn.vendor_prices, want_stone=True)
    want = _stones_to_mine(role, turn, target, mine, len(free))

    if want > 0 and mine is not None:
        if role.pos.dist(mine) <= 1:
            if _emit(cmds, role, actions.Collect, mine):
                return
        # 还没走到矿边：继续走。走不到（或采集被拦）就落到下面去砌墙，别杵着
        elif _step(role, mine, turn, cmds, claimed, sites):
            return

    if role.stone >= WALL_COST:
        # **认领要发生在动身之前**（与建武器同一条做法）：等到砌完再登记的话，
        # 两个工人会在同一回合里都奔着 `target` 去，白走一路、还多花一块石头。
        sites.add(target)
        if role.pos.dist(target) <= 1:
            # 与建武器同一条契约：`step_toward` 停在贴着目标的一格，那正是 `build` 的站位
            if _emit(cmds, role, actions.Build, WALL, target):
                return
        elif _step(role, target, turn, cmds, claimed, sites):
            return
    # 手里没石头、又没时间采了 ⇒ 什么都不发。空指令合法且不计异常（CLAUDE.md 硬约束 2）


def _ring(turn: Turn) -> tuple[Pos, ...]:
    """按优先级排的围墙格。基地没了 ⇒ 空（没有可建造区就不砌）。

    既不在 `wall_cells` 里、也不挡路的格才算候选 —— 但**自己人站的那一格例外**。

    工人站在待砌的墙格上只是**路过**（下一回合就走开），可那一格会因为有单位而入 `blocked`。
    若把它从表里划掉，另一个工人的 `free[0]` 就整体后移一格、掉头去砌更靠后的墙；
    等它一挪窝，目标又变回来，两人对着改目标来回踱步 —— **实测死循环：一格不砌，
    两个工人两格之间转到天黑**。已砌的墙 / 中立元素 / 机器人照旧排除（那些不是"路过"）。
    """
    station = turn.map.station
    if station is None:
        return ()
    #: 我方角色当前站的格（角色能走的都在这 —— 换边、多加角色都不用改）
    mine = {r.pos for r in turn.roles}
    return tuple(
        c for c in wall_cells(station, turn.map.size[0]) if c not in turn.map.blocked or c in mine
    )


def _stones_to_mine(role: Worker, turn: Turn, target: Pos, mine: Pos | None, free: int) -> int:
    """这一趟还该采几块石头 —— 用户给的**回合预算**模型，每回合现算。

    用户的原话：「实时计算白天还有多少回合（赶路回合数+收集回合数+建墙回合数），
    当时间满足的时候尽可能多的采矿，减少来回的回合消耗，给自己留下 5 个回合的容错余量
    回家建墙，注意建墙的回合数也要算在回合数消耗中」。

    记 `s` = 手里已有的石头、`k` = 还要采的块数，把整件事的回合数写出来：

        走到矿(pos→mine) + 采 k 块 + 从矿走回工地(mine→target) + 砌 s+k 座的"挪一格 + 建造"
        = d_mine + k + d_wall + (2(s+k) − 1)      # 到达工地那一步已贴着首格，故 −1
        = d_mine + d_wall + 2s − 1 + 3k           # ⇒ 每多采一块**净**花 3 回合

    令它 ≤ `白天还剩的回合 − 5`，解出 k。

    距离用**切比雪夫**：那是本游戏的移动度量（8 方向、每步 1 回合），空地上精确，
    绕障时会低估 —— `TIME_MARGIN` 就是用来吸收这个误差的。不为此改用 BFS 距离：
    `step_toward` 不返回路径长度，为一个估算去改它的契约不划算。

    最后与"还差几格墙"取小：**没有转移物品的指令**，多采的石头给不了别的工人，
    只能压在自己背包里等第二天补墙 —— 所以只防"采得离谱"，不必精确分账。
    """
    if mine is None:
        return 0
    budget = (
        turn.day_rounds_left
        - TIME_MARGIN
        - role.pos.dist(mine)
        - mine.dist(target)
        - 2 * role.stone
        + 1
    )
    return max(0, min(budget // ROUNDS_PER_STONE, free - role.stone))


def _mine_spare_ore(
    role: Worker, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos], sites: set[Pos]
) -> None:
    """墙砌完了 ⇒ 白天不再闲着，去采**收购价最高**的矿（用户：「然后选择价格最高的矿」）。

    这一趟**没有工地可回**，参照点换成**基地**（夜里的炮位就在基地四周）：白天不够走个
    来回就不动身 —— 黑天里还在赶路等于拿火力换矿石。`TIME_MARGIN` 与 `_stones_to_mine`
    同源，只是把"工地"换成了基地。

    **拿不到收购价就哪儿也不去**（`vendorPrices` 缺失、或三种矿小贩都不收）：
    "价格最高"在没有价格时无从谈起，与 `_gold` / `_size` / `_stone` 同一条降级方向。

    ⚠️ **别指望这一步现在就能换钱**：`sell`（小贩周围一格内把石头/铁/铜换成金币）
    还没实现，采回来的矿现在只是压在背包里 —— 它的价值等那条线做出来才兑现。
    在那之前这一步的收益是"工人反正闲着"，代价是背包占用与离炮位更远。
    """
    station = turn.map.station
    mine = _pick_ore(role.pos, turn.map.ores, turn.vendor_prices, want_stone=False)
    if station is None or mine is None:
        return
    if role.pos.dist(mine) + mine.dist(station) > turn.day_rounds_left - TIME_MARGIN:
        return
    if role.pos.dist(mine) <= 1:
        _emit(cmds, role, actions.Collect, mine)
        return
    _step(role, mine, turn, cmds, claimed, sites)


def _pick_ore(
    pos: Pos, ores: Mapping[Pos, str], prices: Mapping[str, int], *, want_stone: bool
) -> Pos | None:
    """挑一座矿。两种口径（用户策略指导里的两句各占一条）：

    - `want_stone`（手上石头还不够砌完剩下的墙）⇒ **只认石矿，取最近的**
      —— 策略指导：「找石矿应该要去**最近的**」。铁/铜砌不了墙，价格再高也不看一眼。
    - 否则 ⇒ 三种矿里**按小贩收购价从高到低**，同价再取近的（「选择价格最高的矿」）。
      价目表里没有的矿种按 0 算：**小贩不收的矿不值得为它多走一步**，
      顺带也就实现了"一张空价目表 ⇒ 谁也不采"。

    价格取自载荷 `Turn.vendor_prices`（`vendorShopList`），**不写死"铜 > 铁 > 石头"**
    —— 那是样例的价目，而任务书 L386 明说官方消息会让价格波动。

    并列按坐标排：与 `_defend` 的 `(dist, id)` 同一条理由 —— 先后不能取决于 payload
    里的顺序，否则用例复现不了。

    不认领矿：两个工人挤同一座矿的**不同邻格都能采**，只有"冲进同一格"才是白扔动作，
    而那件事已经由 `claimed` 挡住了。
    """
    if want_stone:
        return min(
            (p for p, kind in ores.items() if kind == STONE),
            key=lambda p: (pos.dist(p), p),
            default=None,
        )
    #: `(取负的价, 距离, 坐标)` —— **价排第一**，`min` 出来就是"价最高、同价取近的、再同取坐标最小"
    best = min(
        ((-prices.get(kind, 0), pos.dist(p), p) for p, kind in ores.items()),
        default=None,
    )
    # 价 0 ⇒ 小贩不收，不为它多走一步；一张空价目表也就自然落成"谁也不采"
    return best[2] if best is not None and best[0] < 0 else None


# ── 夜里：回炮位、开火 ──────────────────────────────────────────────
def _defend(
    role: BaseRole, turn: Turn, cmds: dict[str, dict[str, Any]], claimed: set[Pos], taken: set[Pos]
) -> None:
    """夜里：走到最近的一座**还没被本回合别的角色认领**的武器旁，贴着就开火。

    **所有角色都走这里**（含开拓者）—— §4.4 里 `attack` 的可用角色是"全部"。

    **回合号缺失（`round_no < 0`）时一发不发。** `is_day` 把缺失的回合号判成夜里
    （`within = 129`）—— 那个降级方向对"白天不许建造"是**安全**的（少建一座），
    对 `attack` 就反过来了：**白天开火是非法的**（任务书 L122 / 接口文档 L229）。
    同一个降级方向，这里必须掰回来。

    选炮：最近的、没人认领的；**同距离按 `id` 排** —— `Weapon` 带 id，顺手补成全序，
    否则并列时的先后取决于 payload 里的顺序，用例复现不了。

    贴着炮（切比雪夫 ≤1）就**认领并停在这里**，开不开火交给 `_fire`。**不换炮**：
    站哪座炮是走到位那一刻定下的事，每回合重挑一遍会让角色在炮位之间周期性来回走
    （第 8 步 `_ring` 那个死循环的同一类坑）。一人同时只能操一座，所以用 `taken` 去重 ——
    两个角色挤同一座等于有一个白站。

    场上没有武器 ⇒ 不动（空指令合法且不计异常）。
    """
    if turn.round_no < 0:
        return
    for weapon in sorted(turn.weapons, key=lambda w: (role.pos.dist(w.pos), w.id)):
        if weapon.pos in taken:
            continue
        if role.pos.dist(weapon.pos) <= 1:
            taken.add(weapon.pos)
            _fire(role, weapon, turn, cmds)  # 打不了就不发，但人已经站住了
            return
        step = step_toward(role.pos, weapon.pos, turn.map.blocked | claimed, turn.map.size)
        if step is None or not _emit(cmds, role, actions.Move, step):
            continue  # 走不到 / 发不出去 → 换下一座，别为一棵树放弃整片林子
        taken.add(weapon.pos)  # 认领发生在迈步之后：没走成才轮到下一座
        claimed.add(step)
        return


def _fire(role: BaseRole, weapon: Weapon, turn: Turn, cmds: dict[str, dict[str, Any]]) -> bool:
    """贴着炮了：能打就打一发，返回发没发出去。**打不了就什么都不发** ——
    空指令合法且不计异常（`CLAUDE.md` 硬约束 2）。

    - **冷却中不打**：火箭发射台发射后有 3 回合空窗（接口文档 L99）。**字段缺失时不算冷却**
      （`cooldown = -1`；样例三座炮都没有这个字段）—— 否则火箭整晚一炮不开。
    - **射程内没有机器人不打**：打空处是"指令执行失败"（任务书 L508），不致命，
      但白耗一次冷却，还看不出是"没敌人"还是"瞄错了"。
    - 目标 = **射程内血最少的**（用户选定：补刀优先，**距离作平手判定**）。
      ⚠️ **必须先按射程过滤、再取 min** —— 顺序写反就成了"拿全场最弱、但打不着的那个当目标"。
    - **不按"本回合已被别人瞄过"去重**：用户选的就是补刀 = 集火，摊开火力正好相反。
    - 机器人 `health` 缺失（-1）时**按"还活着"算**（`!= 0`，与 `model._destroyed` 同源）：
      打空处只是执行失败，而"一律不打"会让整晚一炮不开 —— 降级方向选前者。
    """
    if weapon.cooldown > 0:
        return False
    reach = [
        r
        for r in turn.robots
        if r.health != 0 and weapon.pos.dist(r.pos) <= weapon.attack_range
    ]
    if not reach:
        return False
    target = min(reach, key=lambda r: (r.health, weapon.pos.dist(r.pos)))
    # **key 是武器 id**，操控角色在报文的 `controllerId` 里（`actions.Attack`）
    return _emit(cmds, role, actions.Attack, str(role.id), target.pos, key=str(weapon.id))


# ── 发指令 ──────────────────────────────────────────────────────────
def _step(
    role: BaseRole,
    goal: Pos,
    turn: Turn,
    cmds: dict[str, dict[str, Any]],
    claimed: set[Pos],
    avoid: Set[Pos] = frozenset(),
) -> bool:
    """朝 `goal` 走一格并登记落脚格。走不到 / 发不出去返回 False。

    `avoid` = 额外要避开的格。工人传**建造格**（免得径直走到建造格**上**去 ——
    那样建完自己站在墙里）；开拓者没有这一层，用默认的空集。
    """
    walk = turn.map.blocked | claimed | avoid
    step = step_toward(role.pos, goal, walk, turn.map.size)
    if step is None or not _emit(cmds, role, actions.Move, step):
        return False
    claimed.add(step)
    return True


def _emit(
    cmds: dict[str, dict[str, Any]],
    role: BaseRole,
    cls: type[actions.BaseAction],
    *args: Any,
    key: str | None = None,
) -> bool:
    """造一条指令放进 `cmds`，成功返回 True。

    默认挂在 `str(role.id)` 下 —— **只有 `attack` 例外**：它的 key 是**武器 id**，
    操控者在报文的 `controllerId` 里（`actions.Attack`）。所以这里开一个 keyword-only 的
    口子，**只有 `_fire` 那一处传 `key`**；`PermissionError` 的兜底保持"只此一处"，不另开出口。

    **越权只丢这一条并告警**，不连坐同回合其他角色 —— 若越权是系统性的，抛出去会变成
    "每回合空指令 → 全队冻结一整局"，现象与 `main3.py` 改名事故一样难排查
    （`CLAUDE.md` 硬约束 3）。`role_type` 是 Action 唯一能自证的权限，所以闸门只能在这里。
    """
    try:
        cmds[key if key is not None else str(role.id)] = cls(role.type_name, *args).to_wire()
    except PermissionError as exc:
        LOGGER.warning("拦下越权动作：角色 %s(%s) %s", role.id, role.type_name, exc)
        return False
    return True
