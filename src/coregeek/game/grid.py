"""几何基元：坐标、方向、距离、寻路、基地几何。**不依赖任何其它模块。**

两个距离口径，**别混用**（第 33 步起）：

- `Pos.dist` = **切比雪夫直线**。用于"谁离得近"这类**选点**（挑最近的矿、最近的炮）。
  不绕障，所以它**不是**能走到的步数。
- `steps_between` = **BFS 真实步数**（绕障）。用于**回合预算**（"这天还来不来得及来回"）。
  ⚠️ 它有一个 `Pos.dist` 给不出的返回值 **-1（不可达）**，调用方必须自己接住。

角色可朝 8 个方向移动；无权重 ⇒ BFS 的步数就是切比雪夫下的最短路长度。
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

    **终点是"贴着 goal 的一格"，不是 goal 本身** —— goal 通常是一格挡路的东西
    （矿、建筑、武器操控位），角色本来就只能站在它旁边。BFS 只在可通行格上展开，
    所以它不会把 `goal` 自己当成落脚点，无需特判。返回的是**从 pos 迈出的第一步**。

    `size` = `(width, height)`：BFS 会往远处探路，所以必须自己挡住地图外
    （越界会怎样文档没写，不走一定安全）；尺寸无效（≤0）时无格可走 ⇒ 单位不动。
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
            # 用新变量：直接改 `first` 会让第二个邻居继承第一个的第一步。
            nxt_first = nxt if first is None else first
            if nxt.dist(goal) == 1:
                return nxt_first
            queue.append((nxt, nxt_first))
    return None


def step_outside(pos: Pos, box: Set[Pos], blocked: Set[Pos], size: tuple[int, int]) -> Pos | None:
    """朝 `box` **外面**走一格（BFS 最短路）；`pos` 已经在外面、或压根走不出去，返回 None。

    与 `step_toward` 同构，差别只在**终止条件**：那里是"贴着某一格"，这里是 `nxt not in box`。
    不能用 `step_toward` 顶替：「盒子外面」不是一个 goal；而且它的契约是"贴着 goal 即到"
    （`dist <= 1 ⇒ None`），拿它测"出不出得去"会把**正贴着门口、而门被堵住**的角色
    误判成"到不了"。

    `box` 是**格子集合**而不是矩形（由 `box_cells` 给）。
    ⚠️ "本来就在外面"与"走不出去"合流成一个 `None` 是**有意的**，代价是
    **"谁在盒子里面"必须由调用方自己筛**（`planner._trapped` 就踩过这个）。
    """
    if pos not in box:
        return None  # 本来就在外面
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
            # 与 `step_toward` 同一个坑：用新变量，别把上一格的第一步漏给下一个邻居
            nxt_first = nxt if first is None else first
            if nxt not in box:
                return nxt_first
            queue.append((nxt, nxt_first))
    return None


def steps_between(start: Pos, goal: Pos, blocked: Set[Pos], size: tuple[int, int]) -> int:
    """走到"**贴着 goal 的一格**"要几步 —— **BFS 真实步数**（绕障）；走不到 ⇒ **-1**。

    与 `step_toward` 同一个终点约定（贴着 goal 一格、goal 自己当障碍）、同一套边界裁剪，
    差别只在它**返回步数而不是第一步**：`start` 已经贴着 goal ⇒ 0。

    **存在的理由**：回炮位必须绕过整面围墙、从背面那道门进来，直线距离会把它低估得离谱
    （第 33 步：所有回合预算从切比雪夫换成它，见模块 docstring）。
    ⚠️ **-1 是本函数独有的失败态** —— `Pos.dist` 永远不会返回它，所以每个调用点都要自己
    决定"不可达怎么办"：一律按"这趟不去了"处理（安全方向：宁可少干一件事，
    不可把回程预算算成负数、把人留在墙外过夜）。
    """
    if start.dist(goal) <= 1:
        return 0  # 已经贴着 goal（含 start 就是 goal）
    width, height = size
    # 队列里存 `(当前格, 从 start 走了几步)` —— 这是与 `step_toward` 唯一的实现差别
    queue: deque[tuple[Pos, int]] = deque([(start, 0)])
    seen = {start}
    while queue:
        cell, depth = queue.popleft()
        for step in STEPS:
            nxt = Pos(cell.x + step.x, cell.y + step.y)
            if nxt in seen or nxt in blocked:
                continue
            if not (0 <= nxt.x < width and 0 <= nxt.y < height):
                continue
            seen.add(nxt)
            if nxt.dist(goal) == 1:
                return depth + 1
            queue.append((nxt, depth + 1))
    return -1


def base_cells(top_left: Pos) -> set[Pos]:
    """基地的 2×2 四格。`pos` 给的是**左上角** ⇒ x 向右增、**y 向下减**。"""
    return {Pos(top_left.x + dx, top_left.y - dy) for dx in (0, 1) for dy in (0, 1)}


def box_cells(base: Pos) -> frozenset[Pos]:
    """整个防御盒子：**36 格** = 基地 4 + 武器环 12 + 围墙环 20。

    即 `x ∈ [bx-2, bx+3]` × `y ∈ [by-3, by+2]`。
    存在的唯一理由是给"会不会被墙关住"提供"里 / 外"的判据（`step_outside` 的 `box`）。
    """
    return frozenset(
        Pos(x, y)
        for x in range(base.x - 2, base.x + 4)
        for y in range(base.y - 3, base.y + 3)
    )


def weapon_cells(base: Pos) -> tuple[Pos, ...]:
    """可建造武器的 12 格 = 基地外圈 4×4 减去基地自身（`x ∈ [bx-1, bx+2]`、`y ∈ [by-2, by+1]`）。

    ⚠️ 公式的来源是图（`docs/pic/build_map.png`），任务书正文没有。
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

    `d` = 机器人来的方向：基地在地图左半 ⇒ `+1`（从 `+x` 来），右半 ⇒ `-1`。
    `far` / `near` = 基地朝机器人一侧 / 背向一侧的那条边（基地占 `[bx, bx+1]`）。
    按**基地坐标**判而不用 `teamOur.type`：换边后基地会挪，按坐标判会自动跟着翻。

    **只此一处**：`wall_cells` 与 `weapon_sites` 的正面/背面都从这里取。
    """
    d = 1 if base.x * 2 < width else -1
    return (d, base.x + 1, base.x) if d > 0 else (d, base.x, base.x + 1)


def wall_cells(base: Pos, width: int) -> tuple[Pos, ...]:
    """可砌围墙的 **18 格，按建造优先级排**（**背面只留中间 2 格当门**）。

    6×6 边框里既不属于基地 4 格、也不属于武器环 12 格的格子，共 20 格；砌其中 18 格：
    **正面列 6（含封口那一格）+ 两侧行各 5（含背面两角）+ 背面 2**。
    正面/背面由 `_front_back` 给（左半基地 ⇒ 正面 `x = bx+3`、背面 `x = bx-2`）。

    **顺序 = 沿环走一圈**，起点在正面列的一端、终点紧挨封口格（零新机制，就是元组的次序）：
    正面列（一端扫到另一端）→ 一侧行（正面 → 背面）→ 背面自上而下
    （**先砌中间那 2 格把门收窄**、穿过门、落到另一侧的背面角）→ 另一侧行（背面 → 正面）
    → **封口格**。
    ⚠️ 这不是"顺手排的"：**走一圈的绕行量最小**，而工人一天只有 70 回合。同一份合成局面实测
    （`BuildWallTest`，矿在 9 步外）：旧的"正面列 → 顶行 → 底行 → 背面 → 封口格"跑完 70 回合
    只砌上 **17** 格、**封口格当天没砌上**（正面整晚开着口）；走一圈 **18** 格全砌上，
    第 65 回合收官、手里正好剩 0 块石头。差的就是那些横穿盒子再折回来的来回。

    **背面中间 2 格（基地纵深中心那一行及其下一行）永远是空地**（`door_cells` 给的就是它）：
    它是工人进出、采石、
    跑商店的唯一通道。环一闭合就只剩这一处能进出 ⇒ **门被单位堵死时里面的人出不来**
    （第 34 步起有 `remove` 补救：`planner` 拆一格放人、当天当临时门、天黑前补回；
    第 33 步把门从"整列 6 格"收成这 2 格：入口从 6 路并行变 2 路串行，
    机器人只能挤在同一处进，火箭溅射与加特林双弹的价值都翻倍）。
    ⚠️ 门那 2 格里**没有任何建筑**，所以能堵门的只有**单位** —— 堵满 2 格比堵满 6 格容易得多，
    `_trapped` 那道闸门因此反而更常真的合上。

    **封口格 = 正面列正中 `(front_x, by)`，排在最后一个** —— 这是"白天开着方便通行、
    天黑前砌上封死"的落地方式，**零新机制**：靠建造优先级表达（`_ring` 保序、
    `_build_walls` 取 `free[0]`），排最后 ⇒ 白天最后才砌它。
    ⚠️ **第 1 天**靠建造顺序表达（排最后 ⇒ 白天最后才砌它）；**第 2 天起环开局就是满的**，
    开口改由 `planner` 的**临时门**机制重开（第 34 步）：工人拆一格当白天的通道、
    `day_rounds_left <= HOLE_PATCH_LEFT` 时再砌回去 —— 语义还是"白天开着、天黑前封死"，
    只是从"排在建墙顺序最后"换成"排到时间窗最后"。**几何与函数一个字没动。**
    ⚠️ 也正因为排最后，**封口格的落位决定了工人一天够不够用** —— 走一圈的排法让它在第 65 回合
    就砌上了（见上），换回"先一侧行再另一侧行"的排法它当天砌不上、整整一晚前面开着口。
    """
    d, far, near = _front_back(base, width)
    front_x, back_x = far + 2 * d, near - 2 * d
    ys = list(range(base.y - 3, base.y + 3))  # 6 格
    xs = list(range(base.x - 2, base.x + 4))  # 6 格
    #: 侧面两条的中间 4 格（两端的角归正面列 / 背面列）
    side = xs[1:-1]

    #: 白天开口、天黑前封上的那一格（正面列正中）
    seal = Pos(front_x, base.y)
    # ① 正面列（迎着机器人）：从一端扫到另一端（跳过封口格），含上下两角。
    #    终点是 `ys[-1]` 那个角，正好接上 ② 的第一格。
    order = [Pos(front_x, y) for y in ys if Pos(front_x, y) != seal]
    # ② `ys[-1]` 那一行：从正面往背面铺，末了补上背面那个角
    order += [Pos(x, ys[-1]) for x in reversed(side)] + [Pos(back_x, ys[-1])]
    # ③ 背面从那个角一路下来：先砌中间 2 格之一（门从整列 6 格收成中间 2 格就靠这两格），
    #    再穿过门、砌另一格，最后落到底行那个角
    order += [Pos(back_x, base.y + 1), Pos(back_x, base.y - 2), Pos(back_x, ys[0])]
    # ④ 另一行：从背面往正面铺，终点紧挨封口格
    order += [Pos(x, ys[0]) for x in side]
    # ⑤ **封口格排最后** ⇒ 白天最后才砌它
    return tuple(order + [seal])


def door_cells(base: Pos, width: int) -> tuple[Pos, ...]:
    """**永远不砌的那 2 格** —— 背面列正中（基地纵深中心那一行及其下一行），盒子唯一的进出口。

    存在理由是给"自己人算不算障碍"一个判据（`planner._walled`）：**能堵门的只有单位**
    （那 18 格墙里没有任何建筑），所以"自己人站在待砌的格子上"这个问题**只在门口有意义**。
    """
    d, _far, near = _front_back(base, width)
    back_x = near - 2 * d
    return (Pos(back_x, base.y), Pos(back_x, base.y - 1))


def weapon_sites(base: Pos, width: int) -> tuple[Pos, ...]:
    """三座武器的**落点，按建造顺序**：后列上角 + 前排两角。

    阵形要求**三个落点各自与基地的一角切比雪夫距离 1** —— 升级券与维修包必须在目标建筑
    周围一格内使用，`attack` 也要求角色站在炮旁 ⇒ 一个角色站在落点上，脚下的炮和旁边的基地
    一够就是两个。正/背面由 `_front_back` 给：

    - **后列上角 `(back_x, by+1)`**：离机器人最远、基地挡在它前面。⚠️ 必须是**上角**，
      不是贴基地下沿的 `(by-1)`：后列操作者的**环内站位**因此全落在顶线一侧
      （左半基地就是 (9,24)/(10,25)），**底行路线**（门 → (9,22) → (10,22) → (11,22)，
      通往前排两角的通道）一个都压不着；而且武器贴着门柱，从外面回来的操作者
      BFS 直接停在门柱上、根本不进走廊。旧阵 `(9,23)` 的环内站位含 (9,22)/(10,22)
      （正在底行路线上），加上前上角操作者站 (12,24)，前下角的两个站位就成了孤岛
      —— **操作者堵死操作者**（第 24 步改阵的动机）。
    - **前排两角 `(front_x, by-2)` 与 `(front_x, by+1)`**：主战线上的两个侧翼位。

    左半基地 `(10, 24)` ⇒ `((9, 25), (12, 22), (12, 25))`。三个都在 `weapon_cells` 里。
    """
    d, far, near = _front_back(base, width)
    back_x, front_x = near - d, far + d
    return (
        Pos(back_x, base.y + 1),
        Pos(front_x, base.y - 2),
        Pos(front_x, base.y + 1),
    )
