"""寻路：8 向 BFS 距离场 + 单步下降。

**为什么是 BFS 而不是 demo 的 A-star**：地图只有 41×32=1312 格，每回合每个角色跑一次
全图 BFS 是微秒级；而"选一个落脚点再走过去"这件事**需要到所有格子的距离**，
A* 只能给单目标距离，反而要跑 N 次。所以这里反过来：一次 BFS，回答所有问题。

✅ 与 demo 的 `grid.next_step` 同构，但修掉了一处语义：
demo 把 `blocked` 里的格子**既**当作不可通行、**又**在 `_stand_cells` 里用它筛落脚点，
导致"站在矿区旁边"这种目标要先于寻路被判定；这里把两件事分开
（`Board.land` 管能不能走，`best_stand` 管站哪）。

⚠️ 不实现"禁止斜穿两障碍"：官方 demo 的 `next_step` 只看落点是否 `blocked`，
从不检查两个正交邻居，因此**斜向穿过两个相邻障碍是合法的**。
任务书 §4.5.4 只规定了切比雪夫距离、未提切角，故以 demo 行为为准。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable

from .grid import NEIGHBOUR_STEPS, Pos

__all__ = ["Board", "INF", "distance_field", "best_stand", "next_step_toward", "path_to"]

#: 不可达哨兵。用 int 而不用 None，是为了让 `min(...)` 之类的比较不用到处判空。
INF = 1 << 30

#: 距离场的爆炸半径上限（防止畸形地图把内存吃光）
_MAX_CELLS = 1 << 20


@dataclass(frozen=True, slots=True)
class Board:
    """某一回合、针对**某一个移动者**的通行视图。

    调用方负责把"自己脚下那几格"从 `blocked` 里剔掉（多格单位如基地：4 格）。
    """

    width: int
    height: int
    blocked: frozenset[Pos]

    def inside(self, pos: Pos) -> bool:
        return 0 <= pos.x < self.width and 0 <= pos.y < self.height

    def land(self, pos: Pos) -> bool:
        """能站上去吗。越界或不可走的格子都算 false。"""
        return self.inside(pos) and pos not in self.blocked

    def excluding(self, cells: Iterable[Pos]) -> "Board":
        """派生一份"额外忽略若干格"的视图（用于规划时把待占用的格子预留出来）。"""
        return Board(self.width, self.height, self.blocked - frozenset(cells))

    def walkable_count(self) -> int:
        return self.width * self.height - len(
            {p for p in self.blocked if self.inside(p)}
        )


def distance_field(
    board: Board,
    start: Pos,
    *,
    extra_blocked: frozenset[Pos] = frozenset(),
    limit: int = INF,
) -> dict[Pos, int]:
    """从 `start` 出发的 8 向 BFS 距离场（每步代价 1）。

    `start` 自身即使在 `blocked` 里也必须能出发——否则单位一旦被墙围住就再也算不出路，
    会被误判成"卡死"而反复重发同一条指令。
    """
    field: dict[Pos, int] = {start: 0}
    if limit <= 0:
        return field

    q: deque[Pos] = deque((start,))
    while q:
        cur = q.popleft()
        d = field[cur]
        if d >= limit:
            continue
        for dx, dy in NEIGHBOUR_STEPS:
            nxt = Pos(cur.x + dx, cur.y + dy)
            if nxt in field:
                continue
            if not board.inside(nxt) or nxt in board.blocked or nxt in extra_blocked:
                continue
            field[nxt] = d + 1
            if len(field) >= _MAX_CELLS:
                return field
            q.append(nxt)
    return field


def best_stand(
    board: Board,
    field: dict[Pos, int],
    target: Pos,
    *,
    prefer: Pos | None = None,
) -> Pos | None:
    """在 `target` 周围挑一个**已经可达**的落脚点，返回它；没有则 None。

    为什么需要它：矿区 / 商店 / 任务点都是 `neutralType` 占位格，**本身不可通行**
    （demo 的 `Turn.land` 明确要求 `neutralType == "land"`），
    所以"去采矿"实际是"走到某矿的相邻格，再对矿格发 collect"。
    若 `target` 自身可通行则优先选它。

    并列时按 `prefer` 就近（通常是基地/集合点）——避免同一目标每次都挑到不同角落。
    """
    if board.land(target) and target in field:
        return target

    candidates = [
        p for p in target.neighbours()
        if board.land(p) and p in field
    ]
    if not candidates:
        return None

    def key(p: Pos) -> tuple[int, int, int, int]:
        d = field[p]
        tie = p.distance(prefer) if prefer is not None else 0
        return (d, tie, p.x, p.y)

    return min(candidates, key=key)


def next_step_toward(
    board: Board,
    start: Pos,
    goal: Pos,
    *,
    extra_blocked: frozenset[Pos] = frozenset(),
) -> Pos | None:
    """朝 `goal` 走一步。返回**本回合该走到的格子**；返回 None = 本回合不该移动。

    None 的三种含义（调用方按 `start.distance(goal)` 区分）：
      1. `start.distance(goal) <= 1` —— 已经就位，该执行 collect / build / attack；
      2. 目标不可达；
      3. 目标格不可通行且周围没有可站的落脚点。

    `goal` 不可通行是**合法**输入（矿区、商店、任务点都是 `neutralType` 占位格）：
    此时会挑一个离 `start` 最近的相邻落脚点，走到那就停。

    ⚠️ 必须是"从 start 迈出的第一步"。用起点做根 BFS 后，**所有邻居的 field 都是 1**，
    所以不能拿 field 值直接挑邻居——必须从 `dest` 沿 field 递减回溯到 field==1 的那格。
    （早先写成"取 goal 的邻居"是错的：那给的是路径的**最后**一步。）
    """
    if start == goal:
        return None
    if start.distance(goal) <= 1:
        return None

    field = distance_field(board, start, extra_blocked=extra_blocked)

    if board.land(goal):
        # 目标本身可通行：只认它。不可达就是不可达，不要在它旁边蹭着不走。
        dest = goal if goal in field else None
    else:
        dest = best_stand(board, field, goal, prefer=start)

    if dest is None:
        return None
    d = field.get(dest, INF)
    if d == INF or d == 0:
        return None
    if d == 1:
        return dest

    # 沿 field 严格递减回溯，直到落在 distance-from-start == 1 的那一格
    cur = dest
    while field[cur] > 1:
        nxt = None
        for dx, dy in NEIGHBOUR_STEPS:
            cand = Pos(cur.x + dx, cur.y + dy)
            if field.get(cand, INF) == field[cur] - 1:
                if nxt is None or _step_key(cand, start) < _step_key(nxt, start):
                    nxt = cand
        if nxt is None:  # 理论不可达；防御性退出
            return None
        cur = nxt
    return cur


def _step_key(cand: Pos, start: Pos) -> tuple[int, int, int]:
    """并列时偏好坐标序，保证同一局面下指令稳定（否则日志无法逐回合比对）。"""
    return (cand.distance(start), cand.x, cand.y)


def path_to(
    board: Board,
    start: Pos,
    goal: Pos,
    *,
    extra_blocked: frozenset[Pos] = frozenset(),
) -> tuple[Pos, ...]:
    """完整路径（含 goal，不含 start）。仅用于**代价估算**，不用于每回合决策。"""
    if start == goal:
        return ()
    field = distance_field(board, start, extra_blocked=extra_blocked)
    if field.get(goal, INF) == INF:
        return ()
    path: list[Pos] = [goal]
    cur = goal
    while cur != start:
        d = field[cur]
        prev = None
        for dx, dy in NEIGHBOUR_STEPS:
            cand = Pos(cur.x + dx, cur.y + dy)
            if field.get(cand, INF) == d - 1:
                if prev is None or _step_key(cand, start) < _step_key(prev, start):
                    prev = cand
        if prev is None:  # 理论不可达；防御性退出
            return ()
        path.append(prev)
        cur = prev
    path.reverse()
    return tuple(path[1:])
