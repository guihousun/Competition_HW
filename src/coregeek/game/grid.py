"""几何基元：坐标、方向、距离、寻路。**不依赖任何其它模块。**

任务书 §4.5.4：距离一律用切比雪夫 `max(|dx|, |dy|)`；角色可朝 8 个方向移动。
"""

from collections import deque
from collections.abc import Set
from typing import NamedTuple


class Pos(NamedTuple):
    x: int
    y: int

    def dist(self, other: "Pos") -> int:
        """切比雪夫距离。"""
        return max(abs(self.x - other.x), abs(self.y - other.y))


#: 8 个移动方向（不含原地）。
STEPS: tuple[Pos, ...] = tuple(
    Pos(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)
)


def step_toward(pos: Pos, goal: Pos, blocked: Set[Pos], size: tuple[int, int]) -> Pos | None:
    """朝 goal 走一格，**BFS 最短路**；已经在 goal 旁边、或压根走不到，返回 None。

    **终点是"贴着 goal 的一格"，不是 goal 本身。** goal 通常是一格挡路的东西
    （矿、建筑、武器操控位），角色本来就只能站在它旁边 —— 走到邻格即收工。
    BFS 只在可通行格上展开，所以它永远不会把 `goal` 自己当成落脚点，无需特判。

    `size` = `(width, height)`（接口文档 §1.2.1）：**BFS 会往远处探路，不像贪心只挪一格**，
    所以必须自己挡住地图外。任务书 L85 那张"阻挡移动"的清单里**没写地图边界**，
    越界会怎样是未知的——但 `(0,0)~(width-1,height-1)` 之外一律不走，一定安全。
    尺寸无效（≤0）时无格可走，返回 None ⇒ 单位不动。

    返回的是**从 pos 迈出的第一步**，不是整条路径。
    """
    if pos.dist(goal) <= 1:
        return None  # 已经贴着 goal（含 pos 就是 goal），再动反而走远
    width, height = size
    # 队列里存 `(当前格, 从 pos 迈出的第一步)`；pos 自己还没有"第一步"，故为 None
    queue: deque[tuple[Pos, Pos | None]] = deque([(pos, None)])
    seen = {pos}
    while queue:
        cell, first = queue.popleft()
        for step in STEPS:
            nxt = Pos(cell.x + step.x, cell.y + step.y)
            if nxt in seen or nxt in blocked:
                continue
            if not (0 <= nxt.x < width and 0 <= nxt.y < height):
                continue
            seen.add(nxt)
            # 注意用新变量：`first` 是**这一格**的，内层循环里每个邻居各算各的。
            # 直接改 `first` 会让第二个邻居继承第一个的第一步。
            nxt_first = nxt if first is None else first
            if nxt.dist(goal) == 1:
                return nxt_first
            queue.append((nxt, nxt_first))
    return None


def base_cells(top_left: Pos) -> set[Pos]:
    """基地的 2×2 四格（接口文档 §1.3.1 注）。

    `pos` 给的是**左上角** ⇒ x 向右增、**y 向下减**。只标一格会让角色一头撞进基地里。
    """
    return {Pos(top_left.x + dx, top_left.y - dy) for dx in (0, 1) for dy in (0, 1)}


def weapon_cells(base: Pos) -> tuple[Pos, ...]:
    """可建造武器的 12 格 = 基地外圈 4×4 减去基地自身。

    ⚠️ **来源是图，不是正文**：任务书没有坐标公式，`docs/pic/build_map.png` 写着
    "中间黄色 2×2：基地 / 绿色 4×4 外圈：可建造武器 / 蓝色 6×6 外圈：可建造围墙"。
    基地 `pos` 是左上角、占 `y` 与 `y-1`，所以 4×4 环是 `x ∈ [bx-1, bx+2]`、
    `y ∈ [by-2, by+1]`，16 − 4 = 12 格。

    样例可作交叉验证：基地 `(10,24)` ⇒ 环含 `(9,24)` / `(9,25)` / `(10,25)`，
    正是样例那三座武器所在（**样例几何整体是手画的，不能当校准依据**，但环这一处对得上）。
    """
    own = base_cells(base)
    return tuple(
        Pos(x, y)
        for y in range(base.y - 2, base.y + 2)
        for x in range(base.x - 1, base.x + 3)
        if Pos(x, y) not in own
    )


def _front_back(base: Pos, width: int) -> tuple[int, int, int]:
    """`(d, far, near)` —— 机器人来的方向，以及基地朝它 / 背它的那两条边。

    `d` = **机器人来的方向**：基地在地图左半 ⇒ `+1`（从 `+x` 来），右半 ⇒ `-1`。
    `far` / `near` = 基地**朝机器人**一侧 / **背向**一侧的那条边（基地占 `[bx, bx+1]`）。

    依据是 `docs/pic/大致地图信息.png`：蓝方基地在左、刷新点在其右、箭头向左；红方镜像。
    **任务书正文没写刷新点** —— 这是唯一依据，首场比赛后按实测翻转。
    按**基地坐标**判而不用 `teamOur.type`：下半场换边后队伍身份不变、基地会挪，
    按坐标判会自动跟着翻。

    **只此一处**：`wall_cells` 与 `weapon_sites` 的正面/背面都从这里取 ——
    围墙环在两条边之外 2 格、武器环在它们之外 1 格。
    """
    d = 1 if base.x * 2 < width else -1
    return (d, base.x + 1, base.x) if d > 0 else (d, base.x, base.x + 1)


def wall_cells(base: Pos, width: int) -> tuple[Pos, ...]:
    """可砌围墙的 **16 格，按建造优先级排**（背面列中间留 4 格缺口）。

    环 = 6×6 边框（`build_map.png` 的蓝圈），即 `x ∈ [bx-2, bx+3]`、`y ∈ [by-3, by+2]`
    里既不属于基地 4 格、也不属于武器环 12 格的格子 —— 再往外那一圈与武器环**零重叠**。

    正面/背面由 `_front_back` 给：左半基地 ⇒ **正面（迎着机器人）是 `x = bx+3`、
    背面是 `x = bx-2`**；右半镜像。顺序 = 正面列（从基地纵深中心向两端铺，正对基地的先砌）
    → 顶行 → 底行 → 背面两角。

    **背面列中间 4 格留空**（用户选定）：环一闭合，工人就进出不得了 —— 既采不了矿，
    也回不到环内操炮。留一段 4 格宽的口子，正面仍完整；钻进来的机器人紧贴着武器列
    （`x = bx-1`）与基地，等于直接撞在火力上。
    """
    d, far, near = _front_back(base, width)
    front_x, back_x = far + 2 * d, near - 2 * d
    ys = list(range(base.y - 3, base.y + 3))  # 6 格
    xs = list(range(base.x - 2, base.x + 4))  # 6 格
    #: 基地纵深中心的 2 倍 —— 用整数比大小，避免浮点
    center = 2 * base.y - 1

    front = sorted(ys, key=lambda y: (abs(2 * y - center), y))
    order = [Pos(front_x, y) for y in front]
    # `xs[1:-1]` 正好排除两端的正面列与背面列；`reversed` = 从正面往背面铺
    for row_y in (ys[-1], ys[0]):
        order += [Pos(x, row_y) for x in reversed(xs[1:-1])]
    order += [Pos(back_x, y) for y in (ys[0], ys[-1])]  # 背面只剩两角，中间是缺口
    return tuple(order)


def weapon_sites(base: Pos, width: int) -> tuple[Pos, ...]:
    """三座武器的**落点，按建造顺序**：后列 1 格 + 前排两角（用户选定的阵形）。

    为什么是这个阵形：**升级券与维修包必须在目标建筑周围一格内使用**
    （任务书 L292「武器/基地/围墙升级券均需在目标建筑周围一格内使用」、
    L314「围墙修复包需要使用者站在待修复围墙一格范围内」），而 `attack` 也要求角色站在炮旁。
    下面三个落点**各自都与基地的一角切比雪夫距离 1**，于是同一个角色站在落点上，
    **脚下的炮和旁边的基地一够就是两个**。

    正/背面由 `_front_back` 给（判据只在那里一处）：

    - **后列 `(back_x, by-1)`**：贴基地下沿，离机器人最远，基地的 2×2 实体挡在它前面。
    - **前排两角 `(front_x, by-2)` 与 `(front_x, by+1)`**：主战线上的两个侧翼位，
      各自贴着基地的 `(bx+1, by-1)` / `(bx+1, by)`（右半场镜像成 `(bx, ...)`）。

    左半基地 `(10, 24)` ⇒ `((9, 23), (12, 22), (12, 25))`；右半 `(30, 10)` ⇒
    `((32, 9), (29, 8), (29, 11))`。三个都在 `weapon_cells` 里 —— 否则 `build` 落点非法。
    """
    d, far, near = _front_back(base, width)
    back_x, front_x = near - d, far + d
    return (
        Pos(back_x, base.y - 1),
        Pos(front_x, base.y - 2),
        Pos(front_x, base.y + 1),
    )
