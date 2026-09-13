"""地图：一张 `width×height` 的格子矩阵，每格只装一个**类别**。

**为什么要有这个文件。** 以前这些信息散在 `Turn.blocked`（一个扁平 `frozenset[Pos]`）和
`Turn.mines` 里，而且 `model.load` 对同一份 `mapInfo` **解析了两遍**——挡路的解析与矿的解析
各走各的。两份真相迟早对不上，而对不上的症状是"以为能走、其实撞墙"。
现在只有一份：`cells`。

三个用途（`Turn.map` 字段的存在理由）：

1. **寻路** —— 只问 `blocked`，不问格子里是什么。**不变量：非空即挡路**（任务书 L85 那张清单）。
2. **打印日志调试** —— `render()` 出图。出事故时能看见当时的地形，而不是只看见一条 `move`。
3. **建造** —— `station` 给出可建造环的原点（接口文档里**没有**可建造区字段，只能这样推），
   `weapons` 数已建了几座、分别在哪一类。`stones` 同理，是采石那条线的料源。

**每格只有一个类别，不含属性**：`health` / `level` / `attackRange` / `backpack` / `cooldown`
一概不进来——目前没有任何决策读它们，且其中几个是"文档有、样例没有"的可选字段
（`cooldown` / `PlayerTask.coldDownRounds` / `timeoutRounds` / `RobotRole.targetTeam`），
读进来就得先替它们定默认值。等真有消费者再加。
"""

from collections.abc import Mapping

from .grid import Pos, base_cells

#: 空格子。也是"**不**挡路"的唯一表示。
EMPTY = ""

#: 敌方单位的类别前缀。**必须区分敌我**：两方都有 `wall` / `worker` / `station`，
#: 同一个字符串在网格里撞车就看不出是谁的了。
#: ⚠️ `teamEnemy.roles` 是**逐回合观测而不是固定名册** —— 敌方角色/武器离开视野就消失，
#: **消失 ≠ 被摧毁**（任务书 L97）。所以这些格子的存在与否不能当战损解读。
ENEMY_PREFIX = "enemy:"

#: 机器人的类别前缀。机器人不分敌我（它两边都打），但要和我方单位分开。
ROBOT_PREFIX = "robot:"

#: 石矿。`stones` 只挑它 —— 采石是第 1 天防御线的料源（`build` 需背包有石头）。
STONE = "stone"

#: 基地（接口文档 roleType 表）。**它是 2×2**，`pos` 只给左上角，见 `Map.__init__`。
STATION = "station"

#: 可建造的武器类别（任务书 §4.5.1）。`Map.weapons` 按它们分组 —— 敌方的带 `enemy:` 前缀，
#: 落不进那张表。
WEAPON_KINDS = ("gatling", "railgun", "rocket")

#: 单位/角色 → `(我方字符, 敌方字符)`。**大小写区分敌我**。
_RENDER_SIDED: dict[str, tuple[str, str]] = {
    STATION: ("s", "S"),
    "gatling": ("g", "G"),
    "railgun": ("r", "R"),
    "rocket": ("k", "K"),
    # 墙是唯一的例外：用字母读起来太像单位了
    "wall": ("#", "%"),
    "worker": ("w", "W"),
    "pioneer": ("p", "P"),
}

#: 中立元素 → 字符。**没有敌我之分**，所以不参与上面的大小写规则 ——
#: `weaponShop` 用 `$` 而不是大写 `V`，正是为了不落进"大写 = 敌方"里。
#: 四个任务点分 `1`/`2`/`3`/`4`，因为 `zones` 里**两队任务点同时存在**（样例即如此），
#: 挑战者与防守方同号会撞车。
_RENDER_NEUTRAL: dict[str, str] = {
    "stone": "o",
    "iron": "i",
    "copper": "c",
    "vendor": "v",
    "weaponShop": "$",
    "challengerTaskPoint1": "1",
    "challengerTaskPoint2": "2",
    "defenderTaskPoint1": "3",
    "defenderTaskPoint2": "4",
}

#: 机器人一律 `x`：**不分敌我，也不分体型**。样例里四只 `smallRobot`/`middleRobot`/
#: `largeRobot`/`bossRobot` 画出来是一个样子 —— 要看体型去读 `cells`，那才是真相。
_ROBOT_CHAR = "x"

#: 表外类别。它**在网格里照样挡路**，只是画不出来。
_UNKNOWN_CHAR = "?"


def _inside(pos: Pos, size: tuple[int, int]) -> bool:
    """这一格在 `width × height` 之内吗？`size` = `(width, height)`。"""
    return 0 <= pos.x < size[0] and 0 <= pos.y < size[1]


def _char(kind: str) -> str:
    """类别 → 打印用的**单个**字符（41 列刚好一行放得下）。

    **这张表是有损的**，所以 `cells` 才是真相、`render()` 只是给人看的。
    """
    if not kind:
        return "."
    if kind.startswith(ROBOT_PREFIX):
        return _ROBOT_CHAR
    name = kind[len(ENEMY_PREFIX) :] if kind.startswith(ENEMY_PREFIX) else kind
    pair = _RENDER_SIDED.get(name)
    if pair is not None:
        return pair[1] if kind.startswith(ENEMY_PREFIX) else pair[0]
    return _RENDER_NEUTRAL.get(kind, _UNKNOWN_CHAR)


class Map:
    """一张格子矩阵。由 `protocol.model` 从 payload 构造，策略只读它。"""

    def __init__(self, size: tuple[int, int], entries: Mapping[Pos, str]) -> None:
        """`size` = `(width, height)`；`entries` = 非空格子 `{坐标: 类别}`（**稀疏**）。

        **稠密矩阵在这里铺**，调用方只管"这个坐标是什么类别"——那正是 `model.load`
        扫 payload 时天然会得到的形状，于是铺矩阵只有这一处实现；合成局面的测试也能
        一行造出地图，不必手搓嵌套 tuple。

        矩阵按 `[y][x]` 索引、**y 向上**（任务书 L33：原点在左下角）。用坐标序索引就
        不会在遍历时错位，代价只是 `render()` 里要 `reversed()` 一次。

        尺寸无效（≤0）时矩阵为空 ⇒ `blocked` 也是空集 ⇒ 寻路无格可走 ⇒ 单位不动。
        **这是故意的**：拿不到尺寸就没有一格算在地图内，比走出地图边界吃一条异常划算。
        """
        width, height = size
        self.size = (width, height)
        if width <= 0 or height <= 0:
            self.cells: tuple[tuple[str, ...], ...] = ()
            self.blocked: frozenset[Pos] = frozenset()
            self.stones: frozenset[Pos] = frozenset()
            self.weapons: dict[str, frozenset[Pos]] = {}
            self.station: Pos | None = None
            return

        grid = [[EMPTY] * width for _ in range(height)]
        for pos, kind in entries.items():
            if kind and _inside(pos, self.size):
                # 越界坐标**静默丢弃**：payload 说墙在地图外时，信地图不信它
                grid[pos.y][pos.x] = kind

        # 基地是 2×2 而 pos 只给**左上角**（接口文档："**双方**基地大小为 2*2"）。
        # 展开放在铺格**之后**，让基地永远铺满四格并**盖住**任何声称站在基地里的单位 ——
        # 那是 payload 自相矛盾，宁可信基地：只标一格会让角色一头撞进基地里。
        station: Pos | None = None
        for pos, kind in entries.items():
            if kind not in (STATION, ENEMY_PREFIX + STATION):
                continue
            if kind == STATION:
                station = pos  # 我方基地只有一座；它是所有可建造坐标的原点
            for cell in base_cells(pos):
                if _inside(cell, self.size):
                    grid[cell.y][cell.x] = kind
        self.cells = tuple(tuple(row) for row in grid)
        self.station = station

        blocked: set[Pos] = set()
        stones: set[Pos] = set()
        weapons: dict[str, set[Pos]] = {}
        for y, row in enumerate(self.cells):
            for x, kind in enumerate(row):
                if not kind:
                    continue
                pos = Pos(x, y)
                blocked.add(pos)
                if kind == STONE:
                    stones.add(pos)
                elif kind in WEAPON_KINDS:
                    weapons.setdefault(kind, set()).add(pos)
        self.blocked = frozenset(blocked)
        self.stones = frozenset(stones)
        self.weapons = {k: frozenset(v) for k, v in weapons.items()}

    def render(self) -> str:
        """可打印的图：`height` 行 × `width` 列，**行自上而下 = y 由大到小**。

        y 向上而终端从上往下印，所以这里必须翻一次。**翻反了不会让任何单测挂** ——
        图上的一切还是"看着像张地图"（测试和实现可能一起错），只能靠肉眼跟任务书
        的示意图对一次。

        **刻意不接日志**：`handle` 每回合打 32 行、1300 回合就是 4 万行。需要时手工调。
        """
        return "\n".join("".join(_char(k) for k in row) for row in reversed(self.cells))
