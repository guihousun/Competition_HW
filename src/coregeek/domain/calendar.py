"""昼夜与回合预算。**roundNo 的纯函数**，不依赖 infra。

设计见 docs/design/code-design.md §3.2 第 1 条：昼夜本来被塞进 infra/clock.py，
但那样 domain 为了拿"白天/夜晚"就得 import infra，破坏分层。这里保持纯函数。

✅ 样例 roundNo=85 时恰好是夜晚且有机器人 → 公式已被交叉验证，**不要改**。
"""

from __future__ import annotations

from ..infra import config

__all__ = [
    "within",
    "day_index",
    "is_day",
    "is_night",
    "rounds_left_in_day",
    "rounds_until_night",
    "within_half",
    "rounds_left_in_half",
    "days_remaining_in_half",
]

_ROUNDS = config.ROUNDS_PER_DAY
_DAY = config.DAY_ROUNDS
_HALF = config.HALF_ROUNDS


def within(round_no: int) -> int:
    """当天内的第几回合，1..130。接口文档：`within = (round-1) % 130 + 1`。

    ✅ 样例 roundNo=85 落在 71..130（夜晚）且场上确有机器人 → 公式已交叉验证。
    """
    return (round_no - 1) % _ROUNDS + 1


def day_index(round_no: int) -> int:
    """第几个游戏日，从 1 开始（共 10 天）。"""
    return (round_no - 1) // _ROUNDS + 1


def within_half(round_no: int) -> int:
    """本半场内的第几回合，1..1300。

    ❓任务书只说"每场比赛进行两轮、下半场互换位置"，**没说 roundNo 在下半场是否重置**，
    接口文档也只写"当前回合数"。这里用取模，**两种语义下答案相同**：
      - roundNo 重置 → `(r-1)%1300+1 == r`
      - roundNo 连续（1..2600）→ 下半场自然回到 1..1300
    所以不需要赌哪种语义。130/1300 的整倍数关系也保证 `within()` 在两种语义下一致。
    """
    return (round_no - 1) % _HALF + 1


def is_day(round_no: int) -> bool:
    return within(round_no) <= _DAY


def is_night(round_no: int) -> bool:
    return not is_day(round_no)


def rounds_left_in_day(round_no: int) -> int:
    """含当前回合在内，今天还剩几个回合。"""
    w = within(round_no)
    return _DAY - w + 1 if w <= _DAY else 0


def rounds_until_night(round_no: int) -> int:
    """距离入夜还有几个回合（白天用）。已经入夜则返回 0。"""
    return rounds_left_in_day(round_no)


def rounds_left_in_half(round_no: int) -> int:
    """含当前回合在内，本半场还剩几个回合（1..1300）。

    注意这是**排期上限**，不是保证：基地被摧毁、或对手提前出局都会让半场提前结束。
    因此调用方只能把它当"最迟还有这么久"来用。
    """
    return _HALF - within_half(round_no) + 1


def days_remaining_in_half(round_no: int) -> int:
    """本半场还剩几个完整/不完整的游戏日（1..10）。

    早先叫 `halves_remaining`，但算出来的其实是**天数**，名字与语义不符，
    已改名以免在调度里被误当作"还剩几个半场"。
    """
    return -(-rounds_left_in_half(round_no) // _ROUNDS)
