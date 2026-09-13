"""地图：一张 `width×height` 的格子矩阵，每格只装一个**类别**。

**为什么要有这个文件。** 以前这些信息散在 `Turn.blocked`（一个扁平 `frozenset[Pos]`）和
`Turn.mines` 里，而且 `model.load` 对同一份 `mapInfo` **解析了两遍**——挡路的解析与矿的解析
各走各的。两份真相迟早对不上，而对不上的症状是"以为能走、其实撞墙"。
现在只有一份：`cells`。

三个用途（`Turn.map` 字段的存在理由）：

1. **寻路** —— 只问 `blocked`，不问格子里是什么。**不变量：非空即挡路**（任务书 L85 那张清单）。
2. **打印日志调试** —— `render()` 出图。出事故时能看见当时的地形，而不是只看见一条 `move`。
3. **建造** —— `station` 给出可建造环的原点（接口文档里**没有**可建造区字段，只能这样推）。
   `ores` 是采矿那条线的料源（**矿点 → 矿种**，三种矿各值多少钱在 `Turn.vendor_prices`）。

**每格只有一个类别，不含属性**：`health` / `level` / `attackRange` / `backpack` / `cooldown`
一概不进来，且其中几个是"文档有、样例没有"的可选字段
（`PlayerTask.coldDownRounds` / `timeoutRounds` / `RobotRole.targetTeam`），
读进来就得先替它们定默认值。等真有消费者再加 —— `attackRange` / `cooldown` 第 10 步有了
消费者，它们落在 `world.Weapon` 上（**带 id 的名册**），不是这里的网格。

⚠️ **这里曾经有一个 `Map.weapons`（`dict[类别, 坐标集]`），第 10 步删了。** 它没有 id、
没有射程、没有冷却，而 `attack` 三样都要；留着就是"哪些武器是我们的"存在两份真相。
现在 `Map` 只回答"这一格是什么地形"，武器名册归 `Turn.weapons`（`world.py`）。
"""

from collections.abc import Mapping
from types import MappingProxyType

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

#: 三种矿（接口文档 §1.2.1 的 `neutralType`）。`collect` 采的就是这三种
#: （任务书 §4.4：「需要在石矿、铁矿、铜矿周围一格内使用，每回合获取相应的石头/铁/铜*1」）。
#: 小贩/武器商店/任务点**不是矿**，但一样挡路（它们在 `blocked` 里）。
ORE_KINDS = frozenset({"stone", "iron", "copper"})

#: 石矿。**围墙唯一的料源**（`build` 围墙要求背包里有石头）—— 铁/铜再多也砌不了墙，
#: 所以"手上石头还不够砌墙"时只认它。三种矿各值多少钱看 `Turn.vendor_prices`。
STONE = "stone"

#: 基地（接口文档 roleType 表）。**它是 2×2**，`pos` 只给左上角，见 `Map.__init__`。
STATION = "station"

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

#: 类别 → 中文名。**只有这一份**，`LEGEND` 由它生成。
#:
#: 为什么不直接把对照表手写成字符串：那样"哪个字符代表什么"就有了**两份真相**
#: （上面的字符表 + 手写的图例），加了新中立元素没人记得改图例，而图例写错的症状是
#: **复盘时看错阵营** —— 比看不懂更糟。生成之后断言只剩一句集合相等（见 tests）。
#:
#: ⚠️ 四个任务点按**阵营**命名，不是"我方/敌方"：`1`/`2` 是 `challengerTaskPoint*`、
#: `3`/`4` 是 `defenderTaskPoint*`，两者在 `zones` 里**同时存在**（样例即如此）。
#: 样例里 `teamOur.type == "challenger"` 两套恰好重合，**看不出区别** —— 写"我方任务点"
#: 是一个在样例上永远测不出来的错。我们**可接**的那两个点在 `Turn.task_points`（来自
#: `teamOur.playerTasks`，阵营已滤好），与这里的 `1`-`4` 是两回事。
_NAMES: dict[str, str] = {
    STATION: "基地",
    "gatling": "加特林",
    "railgun": "电磁狙击炮",
    "rocket": "火箭发射台",
    "wall": "围墙",
    "worker": "工人",
    "pioneer": "开拓者",
    "stone": "石矿",
    "iron": "铁矿",
    "copper": "铜矿",
    "vendor": "小贩",
    "weaponShop": "武器商店",
    "challengerTaskPoint1": "挑战方任务点1",
    "challengerTaskPoint2": "挑战方任务点2",
    "defenderTaskPoint1": "防守方任务点1",
    "defenderTaskPoint2": "防守方任务点2",
}

#: 图例每行的**项目数**。按项目数换行而不是按显示宽度 —— 那要算"中文占 2 列"，
#: 而为一行图例养一个宽度函数不值得（地图那边一个字符就是一格，用不上它）。
_LEGEND_PER_LINE = 8


def _inside(pos: Pos, size: tuple[int, int]) -> bool:
    """这一格在 `width × height` 之内吗？`size` = `(width, height)`。"""
    return 0 <= pos.x < size[0] and 0 <= pos.y < size[1]


def _char(kind: str) -> str:
    """类别 → 打印用的**单个**字符。

    ⚠️ **恒返回 1 个字符**（兜底是 `?`，不是 `""`）—— 这是 `render()` 里列能对齐的
    唯一保证，也是 `LEGEND` 能直接拼 `f"{_char(k)}={name}"` 的前提。别引入多字符的记号。

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


def _legend() -> str:
    """字符对照表：`图例：s=基地 g=加特林 …`。**从 `_NAMES` 生成**，不是手写。

    后五项不在 `_NAMES` 里 —— 它们不是"某个类别"，而是 `_char` 的兜底与大小写规则：
    `x`（机器人，不分敌我不分体型）、`.`（空地）、`?`（表外类别）、以及
    **"大写 = 敌方"**这条只写在 `_RENDER_SIDED` 注释里的约定。`%` 要单列：
    **墙是大小写规则的唯一例外**，不写出来没人猜得到。

    写在这里（`_char` 之后）而不是字符表旁边：模块级要调 `_char`，顺序不能反。
    """
    items = [f"{_char(kind)}={name}" for kind, name in _NAMES.items()]
    items += ["x=机器人", ".=空地", "?=未知", "大写=敌方", "%=敌方围墙"]
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
            self.ores: Mapping[Pos, str] = MappingProxyType({})
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
        ores: dict[Pos, str] = {}
        for y, row in enumerate(self.cells):
            for x, kind in enumerate(row):
                if not kind:
                    continue
                pos = Pos(x, y)
                blocked.add(pos)
                if kind in ORE_KINDS:
                    ores[pos] = kind
        self.blocked = frozenset(blocked)
        self.ores = MappingProxyType(ores)

    def render(self) -> str:
        """可打印的**整块**：两行列标尺 + 左侧行号槽 + `height` 行 × `width` 列网格。

        行自上而下 = **y 由大到小**（y 向上而终端从上往下印，这里必须翻一次）。
        **翻反了不会让任何单测挂** —— 图上的一切还是"看着像张地图"（测试和实现可能
        一起错），只能靠肉眼跟任务书的示意图对一次。所以行号槽是**唯一的守卫**：
        它把 y 写进了输出，肉眼或断言都能一眼看出次序对不对。

        **为什么标尺在 `render()` 里而不是让 `app._log` 自己拼**：列宽与缩进由 `size`
        推导，那是**地图自己的知识**；`app` 不该知道"第几个字符才是 x=0"。

        行号槽宽度**由 `height` 推导**、不写死 2 —— 写死 2 在 `height > 100` 时行号
        会撑破槽宽、把网格整体推右一列，而**地图恒 41×32，本地测不出来**。
        同理宽度算的是**字符数**而不是显示列数：`_char` 恒返回 1 字符，
        中文字符一个都没有，两者相等。

        尺寸非法（`cells` 为空）⇒ 空串。**这是既有契约**：没有地图可画时
        `app._log` 那一行会退化成一个空行，而不是抛异常。
        """
        # `not self.cells` 而不是 `self.size`：`cells` 为空才是"没有一格可画"的真判据，
        # 尺寸有效但高/宽为 0 时 `cells` 同样是空 —— 那时 `size` 看着是合法的。
        if not self.cells:
            return ""
        width, height = self.size
        label = len(str(height - 1))
        #: 槽宽 + 分隔符，宽度与每行的行号前缀 `f"{y:>{label}} │ "` **必须**一致，
        #: 否则标尺与网格差一列（`│` 在 UTF-8 终端占 1 列；本项目的日志恒为 UTF-8）
        pad = " " * (label + 3)
        tens = "".join(str(x // 10) if x % 10 == 0 else " " for x in range(width))
        units = "".join(str(x % 10) for x in range(width))
        rows = [pad + tens, pad + units]
        rows += [
            f"{y:>{label}} │ " + "".join(_char(k) for k in self.cells[y])
            for y in range(height - 1, -1, -1)
        ]
        #: 各行**不等长也无所谓**（标尺十位行可能带尾随空格）：不 `rstrip`，
        #: 免得"行尾空格"成为一个要解释的东西。列对齐靠的是前缀等宽，不是行长相等。
        return "\n".join(rows)
