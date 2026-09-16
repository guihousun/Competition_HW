"""局面的领域表示。由 `protocol.model` 从 payload 构造，策略只读它。

字段只带**当前步骤真正用到**的：用到时再加。`equals`/`hash` 由 `NamedTuple` 给。
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Callable, NamedTuple

from .grid import Pos
from .map import COPPER, IRON, STONE, Map
from .roles import BaseRole

#: 日历：130 回合 = 1 天（**白天 70 + 夜晚 60**），共 10 天。
ROUNDS_PER_DAY = 130
DAY_ROUNDS = 70

#: 三种武器工事的 `roleType`。⚠️ 与 `planner.WEAPONS_BY_SITE` **不是一回事**：
#: 那个是"三座炮建在哪、各自是什么"（策略），这个是"哪些 roleType 算武器"（协议）。
WEAPON_KINDS = frozenset({"gatling", "railgun", "rocket"})


#: 摘要里每个列表的**项目数上限**，超出的只报个数（`…+n`）。`Turn.robots` 是逐回合全量推送的，
#: 不截断的话摘要长度就是一个**数据相关的量**，而硬约束 5 那条管道缓冲风险最怕这个。
SUMMARY_MAX_ITEMS = 8

#: 射程大到这个数就打成 `∞`（地图对角线约 52，比它大就是"够得着全图"）。
UNLIMITED_RANGE = 100


def _listed(items: tuple[Any, ...], fmt: Callable[[Any], str], sep: str = " ") -> str:
    """逐项 `fmt` 后用 `sep` 连起来，**超过 `SUMMARY_MAX_ITEMS` 只报个数**。

    空元组返回空串 —— 调用方自己决定"空"怎么说（`无` / `0 台`）。
    """
    shown = sep.join(fmt(i) for i in items[:SUMMARY_MAX_ITEMS])
    rest = len(items) - SUMMARY_MAX_ITEMS
    return f"{shown} …+{rest}" if rest > 0 else shown


class Weapon(NamedTuple):
    """我方一座武器工事 —— 开火与升级要用到的全部信息。

    **`level` 缺省 1**（接口文档：仅建筑持有、初始 1；样例三座全 L1）—— 升级线靠它认
    "还升得动"，摘要也打它（"升级成没成"只有日志能回答）。
    **没有 `health`**：只在解析时用它筛掉已毁的炮，存下来没人读。
    """

    id: int
    """**线上那条指令的 key 就是它** —— 不是操控角色的 id（见 `actions.Attack`）。"""
    kind: str
    pos: Pos
    attack_range: int
    """以**切比雪夫距离**计的射程，取自 payload 的 `attackRange`（**以 payload 为准**）。"""
    cooldown: int
    """冷却**剩余回合数**；只有火箭发射台有（发射后 3 回合空窗），加特林/电磁炮恒 0。
    样例里没有这个字段 ⇒ `model._int` 给 -1 ⇒ `> 0` 为假 ⇒ 照打。"""
    level: int = 1
    """当前等级（1..3）。**默认 1**：字段缺失按文档的"初始等级 1"算，别当成 -1
    （-1 会被升级线当成"还升得动"去买券，白跑一趟）。"""


class Wall(NamedTuple):
    """我方一座围墙实体（第 42 步修墙的判据来源）。

    与武器同住 `teamOur.roles`（`roleType == "wall"`，id 40000 系）。**health 是修墙
    的判据**（半血 ⇒ 修复包/重建）；**level** 决定满血基准（升级后回满血）。
    已毁（health == 0）的墙在 `model._walls` 就丢掉 —— 那是一格缺口，归 `_ring` 管。
    """

    id: int
    pos: Pos
    health: int
    level: int = 1


class Error(NamedTuple):
    """判题器本轮报的一条错（`{errorCode, description}`）。

    **不把 `errorCode` 翻译成文字**：那是会跟文档漂移的第二份真相，
    而 `description` 本来就是判题器的原话。码的含义记在 `CLAUDE.md` 里给读日志的人看。
    """

    code: int
    description: str

    def __str__(self) -> str:
        return f"{self.code}：{self.description}"


class Robot(NamedTuple):
    """一台机器人。**只留这一步用得上的**：打谁只看血量 + 是否打我方。

    `id` / `roleType` / `abnormalState` 都不存 —— 目标位置用的是**坐标**，
    按类型排优先级是另一种打法（用户选定的是"补刀"）。
    `target_team`：该机器人打哪一队（"challenger" / "defender"），用于过滤"不打我们的
    机器人"——火箭射程远（L3 全图），打对方的机器人既浪费火力、又帮对方减轻基地压力。
    字段缺失给空串 ⇒ 当成打我们的（安全降级：不打比打错更糟）。
    """

    pos: Pos
    health: int
    target_team: str = ""


class Turn(NamedTuple):
    round_no: int
    #: 地图信息：一张格子矩阵，每格只有一个类别（见 `map.Map`）。
    map: Map
    #: **只含角色**（开拓者/工人），带 id。建筑不是可操控单位（见 `roles.make`）。
    #: 网格里也标着角色占的格，但那只是为了挡路 —— **要发指令就得有 id，而 id 只在这里**。
    roles: tuple[BaseRole, ...]
    #: 我方金币。建一座武器 25 金。缺失时给 -1 ⇒ 买不起 ⇒ 不建造（故意的降级方向）。
    gold: int
    #: 我方三座武器的**名册**（不是网格里的类别串），由 `teamOur.roles` 解析而来。
    weapons: tuple[Weapon, ...] = ()
    #: 场上**全部**机器人（全图可见、逐回合全量）。白天是空的。
    robots: tuple[Robot, ...] = ()
    #: 本回合**可接取**的己方任务点（冷却中 / 已做完的已在 `model._tasks` 滤掉）。
    task_points: tuple[Pos, ...] = ()
    #: 当前已领取任务的**原文描述**。**非空 = 开拓者手上有任务** —— 这是"任务进行中"的
    #: **唯一判据**（用载荷事实，而不是自己记"谁领了任务"）。
    phase_task: str = ""
    #: 判题器 LLM 的回复。三种可能，全靠 `agent.chat` 解析：一次工具调用 ⇒ 命令进 `executeCmd`；
    #: `<answer>` 包着的答案 ⇒ `submitAnswer`；两者都不像 ⇒ **原文即答案**（兜底）。
    #: ⚠️ 文档**没写**"没发 prompt 时它是什么"（对比 `lastCmdResult` 那句专门写明的空值约定）
    #: ⇒ 必须按**"它可能粘住"**设计。
    llm_resp: str = ""
    #: 上回合 `executeCmd` 的执行结果 —— **沙盒交回来的作业**。文档明说"未发命令时为空字符串"
    #: ⇒ 非空就等于"我上回合发过命令、这是回执"。格式约定**不解析**，非空即原文回灌。
    cmd_result: str = ""
    #: 小贩的**收购价** `{矿种: 单价}`。采哪座矿、卖哪种按它排序 —— **不写死"铜 > 铁 > 石头"**
    #: （那只是样例的价目，官方消息会让价格波动）。查不到的矿种按 0 算（不去采它）；
    #: 缺失 ⇒ 空表 ⇒ 不去采"最值钱的矿"（没有价格就无从挑）。只读：它是逐回合抄来的事实。
    vendor_prices: Mapping[str, int] = MappingProxyType({})
    #: 武器商店的**价目** `{商品名: 单价}`（顶层 `weaponShopList`，样例实证券1=100/券2=150）。
    #: 升级线按它算"买不买得起"—— 与矿价同一条原则：**不写死**。查不到的商品按 0 算
    #: ⇒ 买不起 ⇒ 不跑腿。只读：逐回合抄来的事实。
    shop_prices: Mapping[str, int] = MappingProxyType({})
    #: 判题器**本轮**报的错。空元组 = 本轮没报错。**这是"任务为什么一直失败"唯一的答案来源**。
    errors: tuple[Error, ...] = ()
    #: 上回合各实体的动作**合法性**回执（key 是角色/武器 id、value = 是否合法）。
    #: `errors` 答的是"**为什么**"，这份答的是"**哪一条**" —— 一条格式合法的指令照样可能
    #: **执行失败**（撞墙、打空），那类不计异常、`errors` 里什么都没有，只有这里会翻成 `false`。
    #: **原样保留、不剪枝**（含我们不操控的基地格）：少一条就是少一份证词。
    action_results: tuple[tuple[int, bool], ...] = ()
    #: 我方围墙实体（第 42 步修墙的判据：health 与 level；网格里那份只有类别串）。
    walls: tuple[Wall, ...] = ()
    #: 我方阵营（"challenger" / "defender"），用于过滤"不打我们的机器人"（点 3 优化）。
    #: 缺失 ⇒ 空串 ⇒ 不过滤（全部照打，安全降级）。
    our_team: str = ""
    #: 基地当前血量（夜里基地升级券的判据）。缺失 -1 ⇒ 判"不残血"（不轻举妄动）。
    station_health: int = -1
    #: 基地等级（查满血基准表用）。缺失按 1。
    station_level: int = 1
    #: 官方消息（`worldNews.officialNews`）—— 矿产事件（塌方/停工）的原文（第 42 步）。
    #: `folkLegends` 是宝藏线索，不读。缺失 ⇒ 空串 ⇒ 不查。
    news: str = ""

    @property
    def within(self) -> int:
        """本回合在"一天"里的序号（1..130）。`round_no` 缺失时是 -1 ⇒ 129。"""
        return (self.round_no - 1) % ROUNDS_PER_DAY + 1

    @property
    def is_day(self) -> bool:
        """白天吗？`build` / `remove` **仅白天**可用。

        `within <= 70` 为白天。`round_no` 缺失 ⇒ 129 ⇒ 判成夜晚 ⇒ 不建造。
        ⚠️ `remove` 那半边是**项目口径**、不是任务书原文：§4.4 给 `remove` 那一格
        **没写昼夜**（`build` 写了"仅白天"）。取保守方向（夜里不发）—— 少拆一次不碰红线，
        反过来若实际禁夜就是指令非法。见 `planner._dig` 的闸门。
        """
        return self.within <= DAY_ROUNDS

    @property
    def day_rounds_left(self) -> int:
        """白天还剩几回合，**含本回合**；夜里为 0。

        `planner` 拿它算"这一趟还该采几块石头" —— 按回合计而**不记跨回合状态**。
        """
        return DAY_ROUNDS - self.within + 1 if self.is_day else 0

    def summary(self) -> str:
        """关键事实摘要 —— 图上推不出来的那些：金币 / 武器与射程 / 角色背包 / 机器人血量 / 可接任务点。

        **四块，各占一行、块间不留空行**：`【回合】` / `【我方】` / `【机器】` / `【可接任务点】`。
        首行前面那个 `\\n` 是留给 `logging` 前缀的（时间戳只加在第一条物理行上）。
        放在这里而不是 `app._log` 里：**领域对象自己格式化自己**，`app` 只管装配与红线。

        ⚠️ **不记日志、不留状态、长度有上界**（每个列表 `SUMMARY_MAX_ITEMS` 项）。
        它跑在 `app.handle` 的 `try` 里，抛出去的代价是**整回合退化成空指令** ⇒
        只做字段读取与拼接，不做任何可能失败的运算。

        三条格式化约定（都是为了"日志必须诚实"）：**缺失不等于 0**（`-1` 打成 `?`）；
        **射程** -1 ⇒ `?`、≥ `UNLIMITED_RANGE` ⇒ `∞`；**冷却只在 `> 0` 时出现**
        （`-1` 与 `0` 都是"没有冷却"，与 `model._weapons` 的判据同源）。
        """
        gold = self.gold if self.gold >= 0 else "?"
        round_no = self.round_no if self.round_no >= 0 else "?"
        when = "白天" if self.is_day else "夜里"

        def weapon(w: Weapon) -> str:
            span = (
                "?"
                if w.attack_range < 0
                else ("∞" if w.attack_range >= UNLIMITED_RANGE else str(w.attack_range))
            )
            cooldown = f"c{w.cooldown}" if w.cooldown > 0 else ""
            return f"{w.id} {w.kind}({w.pos.x},{w.pos.y})L{w.level}r{span}{cooldown}"

        def role(r: BaseRole) -> str:
            #: 三种矿**都打**（卖矿那条线要它 —— "工人有铜却不去卖"只有这里能回答）。
            #: 背包里的**其它**物品不打：`summary` 是给固定几行的事实，不是背包转储。
            return (
                f"{r.id} {r.type_name}({r.pos.x},{r.pos.y})"
                f"石{r.bag.get(STONE, 0)}铁{r.bag.get(IRON, 0)}铜{r.bag.get(COPPER, 0)}"
            )

        def robot(b: Robot) -> str:
            return f"({b.pos.x},{b.pos.y})h{b.health}"

        def point(p: Pos) -> str:
            return f"({p.x},{p.y})"

        return "\n".join(
            [
                #: 回合号在最前 —— `logging` 的时间戳前缀只加在**第一条物理行**上
                f"\n【回合】 {round_no}（{when}） ｜ 【金币】 {gold} | "
                f"【武器】 {len(self.weapons)}/{len(self.roles)}："
                f"{_listed(self.weapons, weapon) or '无'}",
                f"【我方】 {_listed(self.roles, role, ' ｜ ') or '无'}",
                f"【机器】 {len(self.robots)} 台：{_listed(self.robots, robot) or '无'}",
                f"【可接任务点】 {_listed(self.task_points, point) or '无'}",
            ]
        )
