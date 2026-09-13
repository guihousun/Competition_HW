"""角色 ↔ 武器工事的分配，以及"该站哪一格"的落脚点选择。

设计见 docs/design/code-design.md §3.2 第 3 条；策略稿 §6.5。

**为什么这件事必须跨回合粘住**：夜间每个回合角色只能走一格。若每回合都按
"谁离哪座炮最近"重新贪心分配，两个角色会在两座炮之间反复横跳 —— 每晚 60 回合
全花在路上，一次攻击都发不出。所以分配一旦确定就**保持**，只在失效
（操控者阵亡 / 武器被拆 / 越界）时才重算。

这也是 `defense.py`（第 6 步）的直接输入：夜战分配先调 `Assignment.reconcile`
拿到配对，再对每对算落点与目标。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .entities import Role
from .grid import Pos
from .path import Board, distance_field
from .world import Turn, board_for

__all__ = ["Assignment", "reconcile", "stand_cell"]


@dataclass(slots=True)
class Assignment:
    """跨回合的配对状态。**可变**，由 `app.py` 持有，每回合原地更新。

    `holders` 的语义是 `role_id -> weapon_id`（谁操控哪座炮），
    与线上报文的 `roleCommandMap` key（**武器** id）方向相反 ——
    这正是 `domain/intent.py` 顶部警告的那处易错点。
    """

    holders: dict[int, int] = field(default_factory=dict)

    def weapon_of(self, role_id: int) -> int | None:
        return self.holders.get(role_id)

    def holder_of(self, weapon_id: int) -> int | None:
        for r, w in self.holders.items():
            if w == weapon_id:
                return r
        return None

    def drop_role(self, role_id: int) -> None:
        self.holders.pop(role_id, None)

    def snapshot(self) -> dict[str, int]:
        """给日志用（JSON 的 key 必须是字符串）。"""
        return {str(k): v for k, v in sorted(self.holders.items())}


def reconcile(
    turn: Turn,
    prev: Assignment,
    *,
    pool: tuple[Role, ...] | None = None,
) -> Assignment:
    """把上一回合的配对修正到本回合，返回**新的** `Assignment`。

    保留规则（粘性）：
      · 操控者还活着、武器还在 → 原样保留（**哪怕看起来换个人更近**）；
      · 操控者阵亡或武器被拆 → 释放；
      · 未被认领的武器 → 分给最近的空闲角色（切比雪夫，不做寻路 ——
        这一步只是"先到先得"的初值，真正的落点由 `stand_cell` 精算）。
    """
    alive_roles = {r.id: r for r in (pool if pool is not None else turn.controllables) if r.alive}
    alive_weapons = {w.id: w for w in turn.weapons if w.alive}

    kept: dict[int, int] = {}
    for rid, wid in prev.holders.items():
        if rid in alive_roles and wid in alive_weapons:
            kept[rid] = wid

    taken_weapons = set(kept.values())
    free = [w for w in alive_weapons.values() if w.id not in taken_weapons]
    idle = [r for r in alive_roles.values() if r.id not in kept]

    # 贪心：按 (距离, id) 排序取最小，保证同局面下结果稳定（否则日志无法逐回合比对）
    pairs = sorted(
        ((r.pos.distance(w.pos), r.id, w.id) for r in idle for w in free),
    )
    for _, rid, wid in pairs:
        if rid in kept or wid in taken_weapons:
            continue
        kept[rid] = wid
        taken_weapons.add(wid)

    return Assignment(kept)


def stand_cell(turn: Turn, role: Role, target: Pos, *, board: Board | None = None) -> Pos | None:
    """`target` 周围一格内、`role` 能**走到**的落脚点；已在位则返回原地。

    一个函数覆盖全部"目标是不可通行格"的交互：
      · 采矿   —— 矿区是占位格（`neutralType`），站不上去；
      · 买卖   —— 小贩 / 武器商店是中立单位，同样占位（任务书 L85）；
      · 领任务 —— 任务点占位，任务书要求"处于任一格周围一格内"；
      · 操控武器 —— 武器格**被武器自己占着**，操控者只能站旁边。

    ⚠️ 因此返回值的契约是"**切比雪夫 ≤1 且 ≠ target**"，没有例外。
    早先版本允许"目标恰好可通行时就直接站上去"，那在操控武器时会出事：
    武器血量归零已从 payload 消失、位置却还在环上时，武器格会算成可通行，
    角色就会朝一个永远走不到的格子走一整晚（指令合法、不记异常、极难发现）。
    """
    if role.pos != target and role.pos.distance(target) <= 1:
        return role.pos  # 已在位，不必重算 BFS

    bd = board if board is not None else board_for(turn, role)
    field = distance_field(bd, role.pos)
    cands = [p for p in target.neighbours() if p != target and bd.land(p) and p in field]
    if not cands:
        return None
    return min(cands, key=lambda p: (field[p], p.distance(role.pos), p.x, p.y))
