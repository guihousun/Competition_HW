"""局面的领域表示。由 `protocol.model` 从 payload 构造，策略只读它。

字段只带当前真正用到的：用到时再加。`equals`/`hash` 由 `NamedTuple` 给。
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Callable, NamedTuple

from .grid import Pos
from .map import COPPER, IRON, STONE, Map
from .roles import BaseRole

#: 日历：130 回合 = 1 天（白天 70 + 夜晚 60），共 10 天。
ROUNDS_PER_DAY = 130
DAY_ROUNDS = 70

#: 三种武器工事的 `roleType`。与 `day.WEAPONS_BY_SITE` 不是一回事：那个是"三座炮建在哪、
#: 各自是什么"（策略），这个是"哪些 roleType 算武器"（协议）。
WEAPON_KINDS = frozenset({"gatling", "railgun", "rocket"})


#: 摘要里每个列表的项目数上限，超出的只报个数（`…+n`）。`Turn.robots` 是逐回合全量推送，
#: 不截断的话摘要长度就是一个数据相关的量。
SUMMARY_MAX_ITEMS = 8

#: 射程大到这个数就打成 `∞`（地图对角线约 52，比它大就是"够得着全图"）。
UNLIMITED_RANGE = 100


def _listed(items: tuple[Any, ...], fmt: Callable[[Any], str], sep: str = " ") -> str:
    """逐项 `fmt` 后用 `sep` 连起来，超过 `SUMMARY_MAX_ITEMS` 只报个数。

    空元组返回空串 —— 调用方自己决定"空"怎么说（`无` / `0 台`）。
    """
    shown = sep.join(fmt(i) for i in items[:SUMMARY_MAX_ITEMS])
    rest = len(items) - SUMMARY_MAX_ITEMS
    return f"{shown} …+{rest}" if rest > 0 else shown


class Weapon(NamedTuple):
    """我方一座武器工事 —— 开火与升级要用到的全部信息。"""

    id: int
    """线上那条指令的 key 就是它 —— 不是操控角色的 id（见 `actions.Attack`）。"""
    kind: str
    pos: Pos
    attack_range: int
    """以切比雪夫距离计的射程（以 payload 为准）。"""
    cooldown: int
    """冷却剩余回合数；只有火箭有。缺失 ⇒ -1 ⇒ 照打。"""
    level: int = 1
    """当前等级（1..3）。缺失按 1 算 —— -1 会被升级线当成"还升得动"。"""


class Wall(NamedTuple):
    """我方一座围墙实体，修墙线的判据来源。

    health 是修墙判据（不到满血 1/5 ⇒ 不完备，升级当修）；level 决定满血基准、也决定用
    哪件东西。已毁（health == 0）的在 `model._walls` 就丢掉 —— 那是一格缺口，归 `_ring` 管。"""

    id: int
    pos: Pos
    health: int
    level: int = 1


class Error(NamedTuple):
    """判题器本轮报的一条错（`{errorCode, description}`）。

    不把 `errorCode` 翻译成文字：那是会跟文档漂移的第二份真相，`description` 本来就是判题器
    的原话。
    """

    code: int
    description: str

    def __str__(self) -> str:
        return f"{self.code}：{self.description}"


class Robot(NamedTuple):
    """一台机器人。只留用得上的字段：打谁只看血量 + 是否打我方。

    `target_team` = 该机器人打哪一队，用于过滤不打我们的机器人 —— 火箭射程远，打对方的
    机器人既浪费火力、又帮对方减轻基地压力。字段缺失给空串 ⇒ 当成打我们的（安全降级）。"""

    pos: Pos
    health: int
    target_team: str = ""


class Turn(NamedTuple):
    round_no: int
    #: 地图信息：一张格子矩阵，每格只有一个类别（见 `map.Map`）。
    map: Map
    #: 只含角色（开拓者/工人），带 id。建筑不是可操控单位（见 `roles.make`）。网格里也标着
    #: 角色占的格，但那只是为了挡路 —— 要发指令就得有 id，而 id 只在这里。
    roles: tuple[BaseRole, ...]
    #: 我方金币。建一座武器 25 金。缺失时给 -1 ⇒ 买不起 ⇒ 不建造（故意的降级方向）。
    gold: int
    #: 我方三座武器的名册（不是网格里的类别串），由 `teamOur.roles` 解析而来。
    weapons: tuple[Weapon, ...] = ()
    #: 场上全部机器人（全图可见、逐回合全量）。白天是空的。
    robots: tuple[Robot, ...] = ()
    #: 本回合可接取的己方任务点（冷却中 / 已做完的已在 `model._tasks` 滤掉）。
    task_points: tuple[Pos, ...] = ()
    #: 当前已领取任务的原文描述。非空 = 开拓者手上有任务 —— 这是"任务进行中"的唯一判据
    #: （用载荷事实，而不是自己记"谁领了任务"）。
    phase_task: str = ""
    #: 判题器 LLM 的回复，全靠 `agent.chat` 解析：工具调用 ⇒ 命令进 `executeCmd`；`<answer>`
    #: 包着的 ⇒ `submitAnswer`；两者都不像 ⇒ 原文即答案（兜底）。文档没写"没发 prompt 时它
    #: 是什么"（对比 `lastCmdResult` 写明了空值约定）⇒ 必须按"它可能粘住"设计。
    llm_resp: str = ""
    #: 上回合 `executeCmd` 的执行结果。文档明说"未发命令时为空字符串"。格式约定不解析，
    #: 非空即原文回灌。
    cmd_result: str = ""
    #: 小贩的收购价 `{矿种: 单价}`。采哪座矿、卖哪种按它排序 —— 不写死"铜 > 铁 > 石头"
    #: （那只是样例的价目，官方消息会让价格波动）。查不到的矿种按 0 算（不去采它）。
    vendor_prices: Mapping[str, int] = MappingProxyType({})
    #: 武器商店的价目 `{商品名: 单价}`（顶层 `weaponShopList`）—— 升级线按它算"买不买得起"。
    #: 与矿价同一条原则：不写死；查不到的商品按 0 算 ⇒ 买不起 ⇒ 不跑腿。
    shop_prices: Mapping[str, int] = MappingProxyType({})
    #: 判题器本轮报的错。空元组 = 本轮没报错。这是"任务为什么一直失败"唯一的答案来源。
    errors: tuple[Error, ...] = ()
    #: 上回合各实体的动作合法性回执（key 是角色/武器 id、value = 是否合法）。`errors` 答
    #: "为什么"，这份答"哪一条" —— 执行失败（撞墙、打空）不计异常、只有这里会翻成
    #: `false`。原样保留、不剪枝（含基地格）：少一条就是少一份证词。
    action_results: tuple[tuple[int, bool], ...] = ()
    #: 我方围墙实体（修墙的判据：health 与 level；网格里那份只有类别串）。
    walls: tuple[Wall, ...] = ()
    #: 我方阵营（"challenger" / "defender"），用于过滤"不打我们的机器人"。
    #: 缺失 ⇒ 空串 ⇒ 不过滤（全部照打，安全降级）。
    our_team: str = ""
    #: 基地当前血量（夜里基地升级券的判据）。缺失 -1 ⇒ 判"不残血"（不轻举妄动）。
    station_health: int = -1
    #: 基地等级（查满血基准表用）。缺失按 1。
    station_level: int = 1
    #: 官方消息（`worldNews.officialNews`）—— 矿产事件（塌方/停工）的原文。
    #: `folkLegends` 是宝藏线索，不读。缺失 ⇒ 空串 ⇒ 不查。
    news: str = ""

    @property
    def within(self) -> int:
        """本回合在"一天"里的序号（1..130）。`round_no` 缺失时是 -1 ⇒ 129。"""
        return (self.round_no - 1) % ROUNDS_PER_DAY + 1

    @property
    def is_day(self) -> bool:
        """白天吗？`within <= 70` 为白天（`build` 仅白天；`remove` 任务书没写，保守也仅
        白天）。`round_no` 缺失 ⇒ 判成夜晚 ⇒ 不建造。"""
        return self.within <= DAY_ROUNDS

    @property
    def day_rounds_left(self) -> int:
        """白天还剩几回合，含本回合；夜里为 0。`planner` 拿它算"这一趟还该采几块石头"。"""
        return DAY_ROUNDS - self.within + 1 if self.is_day else 0

    @property
    def rounds_left(self) -> int:
        """本回合起、这一段（白天或夜里）还剩几回合，含本回合。经济线的预算用它 ——
        夜里清场后也跑同一套差事，`day_rounds_left` 在夜里是 0、问不出东西。"""
        return (
            DAY_ROUNDS - self.within + 1 if self.is_day else ROUNDS_PER_DAY - self.within + 1
        )

    def summary(self) -> str:
        """关键事实摘要 —— 图上推不出来的那些：金币 / 武器与射程 / 角色背包 / 血量 / 任务点。

        四块各占一行（`【回合】`/`【我方】`/`【机器】`/`【可接任务点】`）；首行前面的 `\\n`
        是留给 `logging` 前缀的。长度有上界（`SUMMARY_MAX_ITEMS`）；它跑在 `app.handle` 的
        try 里 ⇒ 只做字段读取与拼接。缺失不等于 0（`-1` 打成 `?`）；射程 ≥
        `UNLIMITED_RANGE` ⇒ `∞`。"""
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
            # 三种矿都打（"工人有铜却不去卖"只有这里能回答）。背包里的其它物品不打 ——
            # summary 是给固定几行的事实，不是背包转储。
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
                # 回合号在最前 —— `logging` 的时间戳前缀只加在第一条物理行上
                f"\n【回合】 {round_no}（{when}） ｜ 【金币】 {gold} | "
                f"【武器】 {len(self.weapons)}/{len(self.roles)}："
                f"{_listed(self.weapons, weapon) or '无'}",
                f"【我方】 {_listed(self.roles, role, ' ｜ ') or '无'}",
                f"【机器】 {len(self.robots)} 台：{_listed(self.robots, robot) or '无'}",
                f"【可接任务点】 {_listed(self.task_points, point) or '无'}",
            ]
        )
