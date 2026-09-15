"""Single auditable monster-wave data source for Issue #19 (local experiment).

Source: the public Issue #19 attachment ``default.xlsx``
(https://github.com/user-attachments/files/32221804/default.xlsx), Sheet1 cells
``B2:H5``. A read-only copy lives at ``reports/issue-19-monsters/original.xlsx``
(sha256 ``d4f9d778...f7a93``); ``reports/issue-19-monsters/cells.json`` is the
per-cell extraction used to fill the tables below.

The attachment leaves some cells **blank**. They stay ``None`` here on purpose:
a blank cell is *unobserved*, not a measured zero, and must never be rewritten.
Runtime code may interpret a blank type as 0 so a full local match can run, but it
labels that assumption and never edits this table. Days 8–10 are entirely absent
from the attachment; the ``observed-seven-days`` profile reuses day 7 only to keep
the local ten-day lifecycle running, and marks every such night as unobserved.

Two explicit profiles exist (``--profile`` / ``profile=``):

* ``observed-seven-days`` (default) — day 1..7 follow the attachment; day 8..10
  have no attachment rows, so a documented *local continuation* adds
  ``LOCAL_FUTURE_SMALL_PER_DAY`` extra small robots per night on top of day 7
  (96/101/106), keeping the known growth direction. Every such night is labelled
  "未观测：本地假设每夜多5只小型" and never called official or statistical.
* ``local-pressure`` — the earlier local experiment, base count
  ``day * pressure + 1`` with a fixed type rule. ``pressure`` only affects this
  profile; it never multiplies the observed counts.

The module returns plain JSON-able values only; it is imported by the simulator
generator and the debug/viewer layer, never by the competition strategy.
"""

from __future__ import annotations

from typing import Any, Iterable

# --------------------------------------------------------------------------
# Source registration (auditable: cell range + sha + provenance)
# --------------------------------------------------------------------------
ISSUE19_SOURCE: dict[str, Any] = {
    "issue": 19,
    "issue_url": "https://github.com/guihousun/Competition_HW/issues/19",
    "attachment_url": "https://github.com/user-attachments/files/32221804/default.xlsx",
    "local_copy": "reports/issue-19-monsters/original.xlsx",
    "cells_copy": "reports/issue-19-monsters/cells.json",
    "sha256": "d4f9d778f11dbf14caab1de82891c8cc5f18892f987cfd9b9a4d1e62727f7a93",
    "sheet": "Sheet1",
    "range": "B2:H5",
    "days_in_attachment": [1, 2, 3, 4, 5, 6, 7],
    "notes": [
        "I2:K5（第8–10天）附件为空：无数据，绝不标为实测。",
        "空单元格保留为 None；运行时按 0 解释时显式标注该本地假设。",
        "A8:A13 的 HP/攻击/射程/积分与官方常数一致，本模块不改动。",
        "A14 “第十回合左右到围墙”是约数观测，不构成速度或必达约束。",
        "A15 排序：靠基地一侧为高级怪，一列最多 9 个。",
    ],
}

# Day -> type -> observed count. ``None`` = the attachment cell was blank.
# Cell coordinates are registered in OBSERVED_CELLS so every number is traceable.
OBSERVED_TYPES: dict[int, dict[str, int | None]] = {
    1: {"smallRobot": 30, "middleRobot": 5, "largeRobot": None, "bossRobot": None},
    2: {"smallRobot": 35, "middleRobot": 10, "largeRobot": None, "bossRobot": None},
    3: {"smallRobot": 40, "middleRobot": 15, "largeRobot": 3, "bossRobot": None},
    4: {"smallRobot": 45, "middleRobot": 20, "largeRobot": 4, "bossRobot": None},
    5: {"smallRobot": 50, "middleRobot": 20, "largeRobot": 4, "bossRobot": 1},
    6: {"smallRobot": 55, "middleRobot": 25, "largeRobot": 5, "bossRobot": 1},
    7: {"smallRobot": 57, "middleRobot": 27, "largeRobot": 5, "bossRobot": 2},
}

_ROWS = {"smallRobot": 2, "middleRobot": 3, "largeRobot": 4, "bossRobot": 5}
_COLUMNS = {1: "B", 2: "C", 3: "D", 4: "E", 5: "F", 6: "G", 7: "H"}
OBSERVED_CELLS: dict[int, dict[str, str]] = {
    day: {kind: f"{_COLUMNS[day]}{row}" for kind, row in _ROWS.items()}
    for day in OBSERVED_TYPES
}

OBSERVED_LAST_DAY = 7
UNOBSERVED_DAYS = (8, 9, 10)
# Days 8–10 are absent from the attachment. To keep the local ten-day lifecycle
# alive *and* respect the known "counts grow with the night" direction, the
# observed profile adds this many extra small robots per unobserved night on top
# of day 7. It is an explicit local continuation assumption, never an official
# formula, and it is not tuned by strategy results.
LOCAL_FUTURE_SMALL_PER_DAY = 5
# The fixed pool must hold the largest wave the default generator can produce.
DEFAULT_POOL_REQUIRED = 106

PROFILE_OBSERVED = "observed-seven-days"
PROFILE_PRESSURE = "local-pressure"
DEFAULT_PROFILE = PROFILE_OBSERVED
PROFILES = (PROFILE_OBSERVED, PROFILE_PRESSURE)

# Order the spawn array highest tier first: near the base are boss/large, then
# middle, then small (Issue #19 A15). Used to keep summoned extras in the same
# order instead of appending them at the far end.
TIER_ORDER = ("bossRobot", "largeRobot", "middleRobot", "smallRobot")
_TIER_RANK = {kind: rank for rank, kind in enumerate(TIER_ORDER)}


def validate_profile(profile: Any) -> str:
    """Reject anything that is not one of the two registered local profiles."""
    if profile is None:
        return DEFAULT_PROFILE
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ValueError(f"unknown wave profile: {profile!r}; expected one of {PROFILES}")
    return profile


def tier_sort(kinds: Iterable[str]) -> list[str]:
    """Stable sort by tier so a nearer slot never holds a lower tier.

    Unknown kinds (e.g. a future robot type) keep their original relative order
    at the end rather than being dropped.
    """
    return sorted(kinds, key=lambda kind: (_TIER_RANK.get(kind, len(TIER_ORDER)), ))


def observed_day(day: int) -> tuple[dict[str, int | None], dict[str, Any]]:
    """Counts and provenance for one day under the observed profile.

    Days 1–7 return the attachment values untouched (``None`` for blank cells).
    Days 8–10 have no attachment data: they return a *local continuation* that
    adds ``LOCAL_FUTURE_SMALL_PER_DAY`` extra small robots per night to day 7, so
    the count keeps the known growth direction without inventing statistics.
    """
    if day in OBSERVED_TYPES:
        return dict(OBSERVED_TYPES[day]), {
            "observed": True,
            "source_day": day,
            "extrapolation": False,
            "cells": dict(OBSERVED_CELLS[day]),
            "note": f"附件实测：default.xlsx Sheet1 {OBSERVED_CELLS[day]['smallRobot']}:"
                    f"{OBSERVED_CELLS[day]['bossRobot']}（第 {day} 天）",
        }
    increment = LOCAL_FUTURE_SMALL_PER_DAY * (day - OBSERVED_LAST_DAY)
    counts = dict(OBSERVED_TYPES[OBSERVED_LAST_DAY])
    counts["smallRobot"] = int(counts.get("smallRobot") or 0) + increment
    return counts, {
        "observed": False,
        "source_day": OBSERVED_LAST_DAY,
        "extrapolation": True,
        "local_future_small_per_day": LOCAL_FUTURE_SMALL_PER_DAY,
        "small_added": increment,
        "seed_counts_day7": dict(OBSERVED_TYPES[OBSERVED_LAST_DAY]),
        "cells": dict(OBSERVED_CELLS[OBSERVED_LAST_DAY]),
        "note": f"未观测：本地续演假设在第7天基础上每夜多 {LOCAL_FUTURE_SMALL_PER_DAY} 只小型"
                f"（第 {day} 天共多 {increment} 只）；非官方统计",
    }


def observed_counts(day: int) -> dict[str, int]:
    """Blank types interpreted as 0, so a ten-day local run can complete."""
    counts, _ = observed_day(day)
    return {kind: int(value or 0) for kind, value in counts.items()}


def _base_pressure_plan(day: int, pressure: int) -> list[str]:
    """The old local pressure experiment: ``day * pressure + 1`` monsters."""
    plan: list[str] = []
    for index in range(day * pressure + 1):
        kind = "middleRobot" if day >= 3 and index % 3 == 0 else "smallRobot"
        if pressure == 3 and day >= 5 and index == 0:
            kind = "largeRobot"
        plan.append(kind)
    return plan


def wave_plan(profile: str, day: int, pressure: int = 1,
              extras: Iterable[str] = ()) -> tuple[list[str], dict[str, Any]]:
    """Build one night's ordered plan plus a provenance record.

    ``extras`` (summon orders and caller-loads) are merged into the same tier
    ordering, so a summoned boss is placed near the base too, not appended at the
    far end. Returns the plan (highest tier first) and a JSON-able annotation.
    """
    profile = validate_profile(profile)
    extra_list = [str(kind) for kind in extras]
    if profile == PROFILE_PRESSURE:
        base = _base_pressure_plan(day, pressure)
        meta: dict[str, Any] = {
            "profile": profile,
            "day": day,
            "observed": False,
            "pressure": pressure,
            "formula": "day * pressure + 1",
            "note": "本地旧压力实验（非官方数量；pressure 仅作用于本 profile）",
        }
    else:
        counts, provenance = observed_day(day)
        base = []
        for kind in TIER_ORDER:
            base.extend([kind] * int(counts.get(kind) or 0))
        meta = {
            "profile": profile,
            "day": day,
            "counts": {kind: int(counts.get(kind) or 0) for kind in TIER_ORDER},
            # For an unobserved day there is no raw attachment value to show; the
            # day-7 starting point is kept separately as `seed_counts_day7`.
            "raw_counts": ({kind: counts.get(kind) for kind in TIER_ORDER}
                           if provenance.get("observed") else
                           {kind: None for kind in TIER_ORDER}),
            "blank_types_zeroed": [kind for kind in TIER_ORDER if counts.get(kind) is None],
            **provenance,
        }
    combined = tier_sort(list(base) + extra_list)
    meta["base_count"] = len(base)
    meta["total_count"] = len(combined)
    meta["extra_count"] = len(extra_list)
    return combined, meta


def source_summary() -> dict[str, Any]:
    """Compact registry for the debug/viewer layer (never official)."""
    return {
        "profiles": list(PROFILES),
        "default": DEFAULT_PROFILE,
        "source": dict(ISSUE19_SOURCE),
        "observed_days": [
            {"day": day, "cells": dict(OBSERVED_CELLS[day]),
             "counts": {kind: OBSERVED_TYPES[day][kind] for kind in TIER_ORDER},
             "total": sum(int(OBSERVED_TYPES[day][kind] or 0) for kind in TIER_ORDER)}
            for day in sorted(OBSERVED_TYPES)
        ],
        "unobserved_days": list(UNOBSERVED_DAYS),
        "unobserved_policy": f"第8–10天附件为空：未观测；本地续演假设每夜多 "
                             f"{LOCAL_FUTURE_SMALL_PER_DAY} 只小型（day8/9/10 = 96/101/106），"
                             f"非官方、非统计推导",
        "local_future_small_per_day": LOCAL_FUTURE_SMALL_PER_DAY,
        "pressure_profile": {"formula": "day * pressure + 1",
                             "note": "旧本地压力实验；pressure 不影响 observed-seven-days"},
    }


__all__ = [
    "ISSUE19_SOURCE", "OBSERVED_TYPES", "OBSERVED_CELLS", "OBSERVED_LAST_DAY",
    "UNOBSERVED_DAYS", "PROFILES", "DEFAULT_PROFILE", "PROFILE_OBSERVED",
    "PROFILE_PRESSURE", "TIER_ORDER", "validate_profile", "tier_sort",
    "observed_day", "observed_counts", "wave_plan", "source_summary",
]
