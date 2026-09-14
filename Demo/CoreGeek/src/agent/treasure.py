"""Treasure summoning (任务书 §4.6, §5.2).

The published text says exactly this much about the mechanic:

* ``summonTreasure`` is pioneer-only and sacrifices 任务用品 in a cell within one
  step, to summon and open the altar treasure;
* the treasure site, its conditions and its timing must be **inferred from the
  rumours** — the material does not state them;
* one map has one treasure: once opened successfully, later attempts earn nothing;
* if both teams satisfy the conditions and issue the command in the same round,
  both receive the reward.

Because the conditions are deliberately not published, this module implements the
*mechanism* exactly and generates the rite itself as a **local fixture**: a
deterministic site, item set and time window per match, published into the state
the same way the task fixture publishes `phaseTask`. That keeps three things true:

* invalid actions spend nothing; legal failed attempts spend the offered items;
* a successful one consumes the items, credits score and gold, and marks the
  treasure taken, so a second attempt earns nothing;
* nothing here is claimed to be the official treasure procedure, and the local
  reward numbers are not official scores.
"""
from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .protocol import Pos, TASK_ITEMS, distance

#: Local fixture values (not official): a treasure is worth a lot, as §5.2 says
#: "宝藏藏有大量积分与金币".
FIXTURE_SCORE = 300
FIXTURE_GOLD = 150
#: How many distinct task items the rite demands, and how long the window is open.
FIXTURE_ITEM_COUNT = 2
FIXTURE_WINDOW = 40


@dataclass
class TreasureRite:
    """One match's treasure: where, with what, and when."""

    site: dict[str, int]
    items: tuple[str, ...]
    opens_at: int
    closes_at: int
    opened: bool = False
    opened_by: tuple[str, ...] = ()
    score: int = FIXTURE_SCORE
    gold: int = FIXTURE_GOLD
    rumour: str = ""
    #: Round in which the treasure was taken. 任务书 §5.2 allows both teams to be
    #: rewarded when both satisfy the conditions *in the same round*, and says one
    #: map has one treasure — so the same round stays open to the other side, and
    #: everything after it is refused.
    taken_round: int | None = None

    def window_open(self, round_no: int) -> bool:
        return self.opens_at <= round_no <= self.closes_at

    def dump(self) -> dict[str, Any]:
        return {
            "site": dict(self.site),
            "items": list(self.items),
            "opensAt": self.opens_at,
            "closesAt": self.closes_at,
            "opened": self.opened,
            "openedBy": list(self.opened_by),
            "takenRound": self.taken_round,
            "score": self.score,
            "gold": self.gold,
            "rumour": self.rumour,
        }


def new_rite(seed: int, *, round_no: int = 1, width: int = 41, height: int = 32,
             task_items: Sequence[str] = TASK_ITEMS) -> TreasureRite:
    """Build the local fixture rite for one match.

    Deterministic from the seed, so a replay and a test agree. The site is placed
    away from the map edges and the window opens later in the match, which is what
    makes the rumour-based approach meaningful: a team has to travel and prepare.
    """
    rng = random.Random(int(seed) * 7919 + 104729)
    site = Pos(rng.randrange(4, max(5, width - 4)), rng.randrange(4, max(5, height - 4)))
    items = tuple(sorted(rng.sample(list(task_items), min(FIXTURE_ITEM_COUNT, len(task_items)))))
    opens_at = round_no + rng.randrange(60, 200)
    rite = TreasureRite(
        site=site.dump(), items=items, opens_at=opens_at,
        closes_at=opens_at + FIXTURE_WINDOW,
    )
    rite.rumour = render_rumour(rite)
    return rite


def attach(state: dict[str, Any], rite: TreasureRite) -> None:
    """Publish the rite the way the real judge would publish a rumour.

    It goes into the simulator-private block *and* into ``worldNews.folkLegends``.
    The second one matters: 任务书 §4.8/§5.2 delivers the clue as a 民间传闻 in the
    world news, and that field survives into the official request — whereas
    ``_demo`` is stripped, so a rite kept only there is invisible to a stateless
    POST and the strategy can never act on it (exactly how the first version
    failed).
    """
    meta = state.setdefault("_demo", {})
    meta["treasure"] = rite.dump()
    news = state.setdefault("worldNews", {})
    if isinstance(news, dict):
        # Synthesise the text when the rite was built field by field: the news is
        # the channel the strategy reads, so it must always carry the clue.
        news["folkLegends"] = rite.rumour or render_rumour(rite)


def render_rumour(rite: TreasureRite) -> str:
    """The rumour text for a rite, in the shape :data:`_RUMOUR` parses back."""
    return (f"民间传闻（本地夹具）：祭坛在 ({int(rite.site.get('x', -1))}, "
            f"{int(rite.site.get('y', -1))}) 附近，需献祭 "
            f"{'、'.join(rite.items)}，第 {rite.opens_at}-{rite.closes_at} 回合有效")


#: The rumour is machine-readable on purpose: a strategy cannot be expected to
#: guess a site from prose, and the official text says the *player* infers the
#: conditions from the rumours — so the local rumour states them plainly.
_RUMOUR = re.compile(
    r"祭坛在\s*\((?P<x>-?\d+),\s*(?P<y>-?\d+)\)\s*附近，需献祭\s*(?P<items>[^，]+)，"
    r"第\s*(?P<opens>\d+)-(?P<closes>\d+)\s*回合有效")


def notes_from_news(state: dict[str, Any], round_no: int) -> dict[str, Any]:
    """Read the treasure plan out of the published world news.

    Returns ``{'known': False}`` when no rumour is present (an imported sample
    snapshot, or a match where the fixture published none).
    """
    news = state.get("worldNews") or {}
    text = str(news.get("folkLegends") or "") if isinstance(news, dict) else ""
    match = _RUMOUR.search(text)
    if match is None:
        return {"known": False, "rumour": text}
    items = tuple(part.strip() for part in match.group("items").split("、") if part.strip())
    opens_at = int(match.group("opens"))
    closes_at = int(match.group("closes"))
    return {
        "known": True,
        "site": {"x": int(match.group("x")), "y": int(match.group("y"))},
        "items": list(items),
        "opensAt": opens_at,
        "closesAt": closes_at,
        "open": opens_at <= int(round_no) <= closes_at,
        "taken": False,          # the news does not report a taken treasure
        "rumour": text,
    }


__all__ = [
    "FIXTURE_GOLD", "FIXTURE_ITEM_COUNT", "FIXTURE_SCORE", "FIXTURE_WINDOW",
    "Refusal", "TreasureRite", "attach", "new_rite", "notes_from_news",
    "rite_of", "summon", "treasure_notes",
]


def rite_of(state: dict[str, Any]) -> TreasureRite | None:
    """Read the rite back out of a state (or an observation carrying `_demo`)."""
    raw = (state.get("_demo") or {}).get("treasure")
    if not isinstance(raw, dict) or "site" not in raw:
        return None
    return TreasureRite(
        site=dict(raw.get("site") or {}),
        items=tuple(str(item) for item in raw.get("items") or ()),
        opens_at=int(raw.get("opensAt") or 0),
        closes_at=int(raw.get("closesAt") or 0),
        opened=bool(raw.get("opened")),
        opened_by=tuple(str(side) for side in raw.get("openedBy") or ()),
        score=int(raw.get("score") or FIXTURE_SCORE),
        gold=int(raw.get("gold") or FIXTURE_GOLD),
        rumour=str(raw.get("rumour") or ""),
        taken_round=(int(raw["takenRound"]) if raw.get("takenRound") is not None else None),
    )


class Refusal(Exception):
    """Raised when a summon is rejected; the caller keeps the round's outcome."""


def _backpack_of(role: dict[str, Any]) -> list[str]:
    return [str(item) for item in (role.get("backpack") or ())]


def summon(state: dict[str, Any], *, side: str, pioneer_id: Any, site: Pos,
           items: Iterable[str], round_no: int) -> dict[str, Any]:
    """Legal sacrifices always spend offered items (interface 2.2).

    site is the action target. Invalid actions raise Refusal and set result0;
    legal attempts return result1-4. Simultaneous-fault precedence is a local
    assumption: location/time, already empty, then offered item mismatch.
    """
    state['lastSummonTreasureResult'] = 0
    roles = (state.get('teamOur') or {}).get('roles') or []
    pioneer = next((r for r in roles if str(r.get('id')) == str(pioneer_id)), None)
    if pioneer is None or pioneer.get('roleType') != 'pioneer' or int(pioneer.get('health') or 0) <= 0:
        raise Refusal('只有存活开拓者可以召唤宝藏')
    if side != (state.get('teamOur') or {}).get('type'):
        raise Refusal('角色不属于当前队伍')
    if distance(Pos.load(pioneer['pos']), site) > 1:
        raise Refusal('献祭目标不在开拓者周围一格内')
    board = state.get('mapInfo') or {}
    if not (0 <= site.x < int(board.get('width') or 41) and 0 <= site.y < int(board.get('height') or 32)):
        raise Refusal('献祭目标在地图之外')
    bag = _backpack_of(pioneer)
    offered = [str(item) for item in items]
    # 任务书 §4.6.3 explicitly allows different task items on different maps.
    # Do not reject inventory names merely because absent from the sample list.
    if any(not item for item in offered) or Counter(offered) - Counter(bag):
        raise Refusal('献祭的任务用品不存在于背包内')
    for item in offered:
        bag.remove(item)
    pioneer['backpack'] = bag
    rite = rite_of(state)
    code = 1
    if rite is None or site != Pos.load(rite.site) or not rite.window_open(int(round_no)):
        code = 2
    elif rite.opened and (side in rite.opened_by or rite.taken_round != int(round_no)):
        code = 4
    elif Counter(offered) != Counter(rite.items):
        code = 3
    state['lastSummonTreasureResult'] = code
    record = {'a': 'treasure', 'side': side, 'pioneer': pioneer_id,
              'site': site.dump(), 'items': offered, 'score': 0, 'gold': 0, 'result': code}
    if code != 1:
        return record
    team = state.setdefault('teamOur', {})
    team['totalScore'] = int(team.get('totalScore') or 0) + rite.score
    team['goldNum'] = int(team.get('goldNum') or 0) + rite.gold
    rite.opened = True
    rite.opened_by = tuple(sorted(set(rite.opened_by) | {side}))
    if rite.taken_round is None:
        rite.taken_round = int(round_no)
    # Do not republish the private rite as a new clue during settlement.
    state.setdefault('_demo', {})['treasure'] = rite.dump()
    record.update(score=rite.score, gold=rite.gold, openedBy=list(rite.opened_by))
    return record


def treasure_notes(state: dict[str, Any], side: str, round_no: int) -> dict[str, Any]:
    """Read public clues and public feedback only, never the hidden rite."""
    notes = notes_from_news(state, round_no)
    notes['taken'] = bool(side == (state.get('teamOur') or {}).get('type')
                          and state.get('lastSummonTreasureResult') in (1, 4))
    return notes


__all__ = [
    "FIXTURE_GOLD", "FIXTURE_ITEM_COUNT", "FIXTURE_SCORE", "FIXTURE_WINDOW",
    "Refusal", "TreasureRite", "attach", "new_rite", "rite_of", "summon",
    "treasure_notes",
]
