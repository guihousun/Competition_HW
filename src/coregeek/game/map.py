"""地图：一张 `width×height` 的格子矩阵，每格只装一个**类别**。

三个用途：**寻路**（只问 `blocked`，不问格子里是什么）、**打印日志**（`render()`）、
**建造**（`station` 是可建造环的原点，接口文档里没有可建造区字段，只能这样推）。

`ores` 是采矿线的料源（矿点 → 矿种），`vendors` 是卖矿线的目标点（`frozenset[Pos]`，
小贩是**格子**不是单位，只能从网格认）。每格只有一个类别、不含属性：血量/等级/射程/
冷却一概不进来，用到时再说（它们落在 `world.Weapon` 那份带 id 的名册上）。
"""

from collections.abc import Mapping
from types import MappingProxyType

from .grid import Pos, base_cells

#: 空格子。也是"**不**挡路"的唯一表示。
EMPTY = ""

#: 敌方单位 / 机器人的类别前缀。**必须区分敌我**：两方都有 `wall` / `worker` / `station`。
#: ⚠️ 敌方角色是**逐回合观测**：离开视野就消失，**消失 ≠ 被摧毁**。
ENEMY_PREFIX = "enemy:"
ROBOT_PREFIX = "robot:"

#: 三种矿的名字（与 `neutralType` / `vendorShopList.name` / 背包里的物品名**同一套词**）。
#: 石矿是围墙唯一的料源；铁/铜目前只有一个用途：卖给小贩。小贩/武器商店/任务点不是矿，但一样挡路。
STONE = "stone"
IRON = "iron"
COPPER = "copper"
ORE_KINDS = frozenset({STONE, IRON, COPPER})

#: 基地与小贩（都是 `neutralType` 里的类别名）。基地是 2×2，`pos` 只给左上角。
STATION = "station"
VENDOR = "vendor"

#: 单位/角色 → `(我方字符, 敌方字符)`。**大小写区分敌我**。
_RENDER_SIDED: dict[str, tuple[str, str]] = {
    STATION: ("s", "S"),
    "gatling": ("g", "G"),
    "railgun": ("r", "R"),
    "rocket": ("k", "K"),
    # 墙用 `#`/`%`：非字母的结构符号，一眼能从单位里分出来。
    "wall": ("#", "%"),
    "worker": ("w", "W"),
    "pioneer": ("p", "P"),
}

#: 中立元素 → 字符。**没有敌我之分**，所以不参与大小写规则（`weaponShop` 用 `$` 而不是大写 `V`）。
#: 四个任务点分 `1`/`2`/`3`/`4`：`zones` 里**两队任务点同时存在**，同号会撞车。
_RENDER_NEUTRAL: dict[str, str] = {
    STONE: "o",
    IRON: "i",
    COPPER: "c",
    VENDOR: "v",
    "weaponShop": "$",
    "challengerTaskPoint1": "1",
    "challengerTaskPoint2": "2",
    "defenderTaskPoint1": "3",
    "defenderTaskPoint2": "4",
}

#: 机器人一律 `x`：**不分敌我，也不分体型**（要看体型去读 `cells`，那才是真相）。
_ROBOT_CHAR = "x"

#: 表外类别。它**在网格里照样挡路**，只是画不出来。
_UNKNOWN_CHAR = "?"

#: 类别 → 中文名。**只有这一份**，`LEGEND` 由它生成。
#: ⚠️ 四个任务点按**阵营**命名而不是"我方/敌方"：`1`/`2` 是挑战方、`3`/`4` 是防守方，
#: 两者在 `zones` 里同时存在；我方可接的那两个点在 `Turn.task_points`。
_NAMES: dict[str, str] = {
    STATION: "基地",
    "gatling": "加特林",
    "railgun": "电磁狙击炮",
    "rocket": "火箭发射台",
    "wall": "围墙",
    "worker": "工人",
    "pioneer": "开拓者",
    STONE: "石矿",
    IRON: "铁矿",
    COPPER: "铜矿",
    VENDOR: "小贩",
    "weaponShop": "武器商店",
    "challengerTaskPoint1": "挑战方任务点1",
    "challengerTaskPoint2": "挑战方任务点2",
    "defenderTaskPoint1": "防守方任务点1",
    "defenderTaskPoint2": "防守方任务点2",
}

#: 图例每行的**项目数**（按项目数换行而不是按显示宽度 —— 那要算"中文占 2 列"，不值得）。
_LEGEND_PER_LINE = 8


def _inside(pos: Pos, size: tuple[int, int]) -> bool:
    """这一格在 `width × height` 之内吗？`size` = `(width, height)`。"""
    return 0 <= pos.x < size[0] and 0 <= pos.y < size[1]


def _char(kind: str) -> str:
    """类别 → 打印用的**单个**字符。

    ⚠️ **恒返回 1 个字符**（兜底是 `?`，不是 `""`）—— 这是 `render()` 列能对齐的唯一保证。
    这张表是**有损的**，所以 `cells` 才是真相，`render()` 只是给人看的。
    空格子打印成**空格**；"这张图有多大、边界在哪"由行号槽 + 两行列标尺负责。
    """
    if not kind:
        return " "
    if kind.startswith(ROBOT_PREFIX):
        return _ROBOT_CHAR
    name = kind[len(ENEMY_PREFIX) :] if kind.startswith(ENEMY_PREFIX) else kind
    pair = _RENDER_SIDED.get(name)
    if pair is not None:
        return pair[1] if kind.startswith(ENEMY_PREFIX) else pair[0]
    return _RENDER_NEUTRAL.get(kind, _UNKNOWN_CHAR)


def _legend() -> str:
    """字符对照表：`图例：s=基地 g=加特林 …`。**从 `_NAMES` 生成**，不手写第二份。

    后五项不在 `_NAMES` 里 —— 它们是 `_char` 的兜底与两条约定：机器人 / 空地 /
    未知 / "大写 = 敌方" / `%`（墙是大小写规则的唯一例外，不写出来没人猜得到）。
    空地那一项写的是字面量 `空格=空地`：直接拼 `_char('')` 会得到 `" =空地"`，看着像少打了一个字符。
    定义在 `_char` 之后：模块级要调它，顺序不能反。
    """
    items = [f"{_char(kind)}={name}" for kind, name in _NAMES.items()]
    items += ["x=机器人", "空格=空地", "?=未知", "大写=敌方", "%=敌方围墙"]
    chunks = [
        " ".join(items[i : i + _LEGEND_PER_LINE])
        for i in range(0, len(items), _LEGEND_PER_LINE)
    ]
    #: 续行缩进到与"图例："同宽，看起来是一个块
    return "\n".join([f"图例：{chunks[0]}", *(f"      {c}" for c in chunks[1:])])


LEGEND = _legend()


class Map:
    """一张格子矩阵。由 `protocol.model` 从 payload 构造，策略只读它。"""

    def __init__(self, size: tuple[int, int], entries: Mapping[Pos, str]) -> None:
        """`size` = `(width, height)`；`entries` = 非空格子 `{坐标: 类别}`（**稀疏**）。

        **稠密矩阵在这里铺**，调用方只管"这个坐标是什么类别"，于是铺矩阵只有这一处实现。
        矩阵按 `[y][x]` 索引、**y 向上**（原点在左下角）；用坐标序索引就不会在遍历时错位，
        代价只是 `render()` 里要 `reversed()` 一次。

        尺寸无效（≤0）时矩阵为空 ⇒ `blocked` 也是空集 ⇒ 寻路无格可走 ⇒ 单位不动（故意的）。
        """
        width, height = size
        self.size = (width, height)
        if width <= 0 or height <= 0:
            self.cells: tuple[tuple[str, ...], ...] = ()
            self.blocked: frozenset[Pos] = frozenset()
            self.ores: Mapping[Pos, str] = MappingProxyType({})
            self.vendors: frozenset[Pos] = frozenset()
            self.station: Pos | None = None
            return

        grid = [[EMPTY] * width for _ in range(height)]
        for pos, kind in entries.items():
            if kind and _inside(pos, self.size):
                # 越界坐标**静默丢弃**：payload 说墙在地图外时，信地图不信它
                grid[pos.y][pos.x] = kind

        # 基地是 2×2 而 pos 只给左上角；展开放在铺格**之后**，让基地铺满四格并盖住
        # 任何声称站在基地里的单位（payload 自相矛盾时，宁可信基地）。
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
        ores: dict[Pos, str] = {}
        vendors: set[Pos] = set()
        for y, row in enumerate(self.cells):
            for x, kind in enumerate(row):
                if not kind:
                    continue
                pos = Pos(x, y)
                blocked.add(pos)
                if kind in ORE_KINDS:
                    ores[pos] = kind
                elif kind == VENDOR:
                    vendors.add(pos)
        self.blocked = frozenset(blocked)
        self.ores = MappingProxyType(ores)
        self.vendors = frozenset(vendors)

    def render(self) -> str:
        """可打印的**整块**：两行列标尺 + 左侧行号槽 + `height` 行 × `width` 列网格。

        行自上而下 = **y 由大到小**（y 向上而终端从上往下印，这里必须翻一次）。
        行号槽宽度由 `height` 推导、不写死 2 —— 写死在 `height > 100` 时会撑破槽宽、
        把网格整体推右一列。标尺放这里而不是让 `app._log` 自己拼：列宽与缩进由 `size` 推导。

        尺寸非法（`cells` 为空）⇒ 空串（既有契约：`app._log` 那一行退化成空行，而不是抛异常）。
        """
        # `not self.cells` 而不是 `self.size`：前者才是"没有一格可画"的真判据。
        if not self.cells:
            return ""
        width, height = self.size
        label = len(str(height - 1))
        #: 槽宽 + 分隔符，宽度与每行的行号前缀 `f"{y:>{label}} │ "` **必须**一致
        pad = " " * (label + 3)
        tens = "".join(str(x // 10) if x % 10 == 0 else " " for x in range(width))
        units = "".join(str(x % 10) for x in range(width))
        rows = [pad + tens, pad + units]
        rows += [
            f"{y:>{label}} │ " + "".join(_char(k) for k in self.cells[y])
            for y in range(height - 1, -1, -1)
        ]
        #: 各行不等长无所谓（列对齐靠前缀等宽，不是行长相等），所以不 `rstrip`
        return "\n".join(rows)
