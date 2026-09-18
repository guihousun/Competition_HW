"""寻路：四个 BFS —— 走一格 / 走上去 / 走出盒子 / 还有几步。

角色可朝 8 个方向移动、无权重 ⇒ BFS 的步数就是切比雪夫下的最短路长度。三个 `step_*` 的骨架
逐字相同，差别只有早退判据与终止判据各一行：`step_toward` 的终点是"贴着 goal 的一格"（goal
自己当障碍）、`step_onto` 的终点是 goal 自己（那格是空地，要踩上去）、`step_outside` 的终点是
"盒外任意一格"；`steps_between` 与 `step_toward` 同终点，多带回一个步数、独有一个 -1 的失败态。
`size` = `(width, height)`：BFS 会往远处探路，必须自己挡住地图外（越界算不算非法文档没写，
不走一定安全）；尺寸无效（≤0）⇒ 无格可走。
"""

from collections import deque
from collections.abc import Set

from .grid import STEPS, Pos


def step_toward(pos: Pos, goal: Pos, blocked: Set[Pos], size: tuple[int, int]) -> Pos | None:
    """朝 goal 走一格（BFS 最短路）；已经贴着 goal、或压根走不到，返回 None。

    终点是"贴着 goal 的一格"，不是 goal 本身 —— goal 通常是挡路的（矿、建筑、武器操控
    位），角色本来就只能站在它旁边；BFS 只在可通行格上展开，无需特判。返回的是从 pos
    迈出的第一步。`size` = `(width, height)`：BFS 会往远处探路，必须自己挡住地图外
    （越界后果文档没写，不走一定安全）；尺寸无效（≤0）⇒ 单位不动。
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


def step_onto(pos: Pos, goal: Pos, blocked: Set[Pos], size: tuple[int, int]) -> Pos | None:
    """朝 goal **自己**走一格（BFS 最短路）；`pos` 已经在 goal 上、或走不到，返回 None。

    与 `step_toward` 的唯一差别是终点：那里要停"贴着 goal 的一格"（goal 通常挡路），这里要
    停在 goal 上 —— 共用的武器操作位就是这种格子（那格是空地，得走上去才贴着两座炮）。
    两个函数不能互换：`step_toward` 到不了这种格子，`step_onto` 停不进挡路的 goal。
    """
    if pos == goal:
        return None  # 已经在上面（含"贴着"以外的情形：这里只能靠 == 判）
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
            if nxt == goal:
                return nxt_first
            queue.append((nxt, nxt_first))
    return None


def step_outside(pos: Pos, box: Set[Pos], blocked: Set[Pos], size: tuple[int, int]) -> Pos | None:
    """朝 `box` 外面走一格（BFS 最短路）；`pos` 已经在外面、或压根走不出去，返回 None。

    与 `step_toward` 同构，差别只在终止条件（这里判 `nxt not in box`）。不能用它顶替：
    「盒子外面」不是一个 goal，而且它"贴着 goal 即到"的契约会把正贴着门口而门被堵住的
    角色误判成"到不了"。`box` 是格子集合（由 `box_cells` 给）。"本来就在外面"与"走不
    出去"合流成一个 None 是有意的 ⇒ 谁在盒子里面必须由调用方筛。
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
    """走到"贴着 goal 的一格"要几步 —— BFS 真实步数（绕障）；走不到返回 -1。

    与 `step_toward` 同一个终点约定（贴着 goal 一格、goal 自己当障碍），差别只在返回步数
    而不是第一步（`start` 已经贴着 goal ⇒ 0）。回炮位必须绕过整面围墙从背面的门进来，
    直线距离会低估得离谱 ⇒ 回合预算一律用它算。-1 是本函数独有的失败态（`Pos.dist`
    永远不会返回它），每个调用点都要自己接住：一律按"这趟不去了"处理（别把回程预算算成
    负数、把人留在墙外过夜）。
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
