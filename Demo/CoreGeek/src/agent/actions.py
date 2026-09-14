"""Registry of the twelve official actions.

Why a registry instead of an if/elif chain:

  * **Generalisation** — a strategy asks for a decision ("sell 3 iron"), and the
    registry resolves it to a legal official command. Adding a behaviour means
    choosing an existing action, not editing a growing branch list.
  * **Pluggability** — strategies and `/debug/action` can register additional
    planners for an action; registration is atomic (a duplicate is refused, so
    an extension can never silently shadow the official builder).
  * **Atomicity** — every planner returns one complete, freshly built command
    dict or ``None``. There is no partial mutation, no shared mutable command,
    and every result is validated against the official field contract before it
    leaves the registry.

This module has no game state: it is the vocabulary, not the policy.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from .protocol import (
    OFFICIAL_ACTIONS,
    Pos,
    accept_task_command,
    attack_command_multi,
    build_command,
    buy_command,
    collect_command,
    drop_command,
    move_command,
    remove_command,
    sell_command,
    submit_answer_command,
    summon_treasure_command,
    use_command,
)

# Fields allowed by 接口文档 §2.2. Anything else is a local bug, not an official
# field, and must not be sent to the judge.
ALLOWED_FIELDS = frozenset({"action", "controllerId", "targetPos", "name", "num",
                            "taskAnswer", "item"})

# Multi-target actions: gatling/rocket take level-many targets, railgun exactly one.
MULTI_TARGET_ACTIONS = frozenset({"attack"})

# Planner keyword names that legitimately differ from the wire field name.
_PLANNER_ALIASES = frozenset({"target", "targets", "controller", "material", "item",
                             "amount", "answer", "items"})


class ActionError(ValueError):
    """Raised when a command would violate the official contract."""


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """Static description of an official action, used for validation and docs."""

    name: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    phase: str = "any"  # any | day | night
    targets: str = "none"  # none | one | many
    summary: str = ""


SPECS: dict[str, ActionSpec] = {
    "move": ActionSpec("move", ("targetPos",), (), ("worker", "pioneer"), "any", "one",
                       "角色八方向移动一格"),
    "attack": ActionSpec("attack", ("targetPos", "controllerId"), (), ("gatling", "railgun", "rocket"),
                         "night", "many", "角色操控武器攻击指定落点"),
    "sell": ActionSpec("sell", ("name",), ("num", "targetPos"), ("worker", "pioneer", "station"),
                       "any", "one", "在小贩旁一格卖出矿石"),
    "buy": ActionSpec("buy", ("name",), ("num", "targetPos"), ("worker", "pioneer", "station"),
                      "any", "one", "在武器商店旁一格购买商品"),
    "build": ActionSpec("build", ("name", "targetPos"), (), ("worker",), "day", "one",
                        "工人建造武器工事或围墙"),
    "remove": ActionSpec("remove", ("targetPos",), (), ("worker",), "day", "one", "工人拆除围墙"),
    "acceptTask": ActionSpec("acceptTask", (), (), ("pioneer",), "any", "none", "开拓者领取任务"),
    "submitAnswer": ActionSpec("submitAnswer", ("taskAnswer",), (), ("pioneer",), "any", "none",
                               "开拓者提交任务答案"),
    "summonTreasure": ActionSpec("summonTreasure", ("targetPos", "item"), (), ("pioneer",), "any",
                                 "one", "开拓者献祭任务用品召唤宝藏"),
    "use": ActionSpec("use", ("name",), ("targetPos",), ("worker", "pioneer", "station"), "any",
                      "one", "使用背包中的消耗品或升级券"),
    "drop": ActionSpec("drop", ("name",), (), ("worker", "pioneer", "station"), "any", "none",
                       "丢弃背包内物品"),
    "collect": ActionSpec("collect", ("targetPos",), (), ("worker",), "any", "one", "工人在矿旁一格采集"),
}
assert set(SPECS) == set(OFFICIAL_ACTIONS), "every official action needs exactly one spec"


def validate(command: dict[str, Any]) -> dict[str, Any]:
    """Check a command against the official contract; return it unchanged.

    Raises ActionError instead of sending something the judge would have to
    reject as an exception (R01: protocol anomalies are not execution failures).
    """
    if not isinstance(command, dict):
        raise ActionError("command must be an object")
    action = command.get("action")
    if action not in SPECS:
        raise ActionError(f"unknown action {action!r}")
    spec = SPECS[action]
    extra = set(command) - ALLOWED_FIELDS
    if extra:
        raise ActionError(f"{action}: non-official fields {sorted(extra)}")
    for field in spec.required:
        if command.get(field) in (None, "", []):
            raise ActionError(f"{action}: missing required field {field}")
    if "targetPos" in command:
        targets = command["targetPos"]
        if not isinstance(targets, list) or not targets:
            raise ActionError(f"{action}: targetPos must be a non-empty list")
        for target in targets:
            if not isinstance(target, dict) or "x" not in target or "y" not in target:
                raise ActionError(f"{action}: targetPos entries must be {{x, y}}")
        if spec.targets == "one" and len(targets) != 1:
            raise ActionError(f"{action}: exactly one target position is required")
        if action == "summonTreasure" and len(targets) != 1:
            raise ActionError("summonTreasure: exactly one target position is required")
    elif spec.targets == "one" and spec.required and "targetPos" not in spec.optional:
        raise ActionError(f"{action}: targetPos is required")
    if "num" in command:
        raw_num = command["num"]
        if isinstance(raw_num, bool):
            raise ActionError(f"{action}: num must be an integer >= 1")
        try:
            amount = int(raw_num)
        except (TypeError, ValueError):
            raise ActionError(f"{action}: num must be an integer") from None
        if amount < 1:
            raise ActionError(f"{action}: num must be an integer >= 1")
    if "controllerId" in command and not isinstance(command["controllerId"], str):
        raise ActionError("attack: controllerId must be a string")
    if action == "summonTreasure":
        items = command.get("item")
        if not isinstance(items, list) or not items:
            raise ActionError("summonTreasure: item must be a non-empty list")
    return command


# --------------------------------------------------------------------------
# Planners: action name -> callable that produces one complete command.
# --------------------------------------------------------------------------

Planner = Callable[..., dict[str, Any] | None]


class ActionRegistry:
    """Atomic, thread-safe registry of command planners."""

    def __init__(self, *, validate_commands: bool = True) -> None:
        self._planners: dict[str, Planner] = {}
        self._lock = threading.RLock()
        self.validate_commands = validate_commands

    # -- registration -----------------------------------------------------
    def register(self, action: str, planner: Planner, *, replace: bool = False) -> Planner:
        """Register a planner atomically.

        A duplicate registration raises unless ``replace=True`` is explicit, so
        a plugin can never silently take over an official action by accident.
        """
        if action not in SPECS:
            raise ActionError(f"cannot register unknown action {action!r}")
        if not callable(planner):
            raise ActionError("planner must be callable")
        with self._lock:
            if action in self._planners and not replace:
                raise ActionError(f"action {action!r} already registered")
            self._planners[action] = planner
        return planner

    def unregister(self, action: str) -> None:
        with self._lock:
            self._planners.pop(action, None)

    def planner(self, action: str) -> Planner | None:
        with self._lock:
            return self._planners.get(action)

    def actions(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._planners)

    def missing(self) -> tuple[str, ...]:
        return tuple(action for action in OFFICIAL_ACTIONS if action not in self.actions())

    # -- planning ---------------------------------------------------------
    def build(self, action: str, **kwargs: Any) -> dict[str, Any] | None:
        planner = self.planner(action)
        if planner is None:
            raise ActionError(f"no planner registered for {action!r}")
        spec = SPECS[action]
        # Any argument that cannot become an official field is dropped loudly
        # rather than silently ignored: a typo must not look like a no-op.
        accepted = set(spec.required) | set(spec.optional) | _PLANNER_ALIASES
        unknown = set(kwargs) - accepted
        if unknown:
            raise ActionError(f"{action}: unexpected planner arguments {sorted(unknown)}")
        # bool is an int in Python; it is never a legal quantity here.
        for name, value in kwargs.items():
            if isinstance(value, bool):
                raise ActionError(f"{action}: {name} must not be a boolean")
        try:
            command = planner(**kwargs)
        except ActionError:
            raise
        except TypeError as error:
            # A missing/renamed argument is a local bug; report it as such
            # instead of letting a raw TypeError look like a judge failure.
            raise ActionError(f"{action}: planner arguments invalid ({error})") from error
        if command is None:
            return None
        if self.validate_commands:
            validate(command)
        return command


def _move(*, target: Pos, **_ignored: Any) -> dict[str, Any]:
    return move_command(target)


def _collect(*, target: Pos, **_ignored: Any) -> dict[str, Any]:
    return collect_command(target)


def _build(*, target: Pos, name: str, **_ignored: Any) -> dict[str, Any]:
    return build_command(target, name)


def _remove(*, target: Pos, **_ignored: Any) -> dict[str, Any]:
    return remove_command(target)


def _attack(*, targets: Sequence[Pos], controller: int | str, **_ignored: Any) -> dict[str, Any]:
    return attack_command_multi(controller, targets)


def _sell(*, material: str, amount: int = 1, target: Pos | None = None, **_ignored: Any) -> dict[str, Any]:
    return sell_command(material, amount, target)


def _buy(*, item: str, amount: int = 1, target: Pos | None = None, **_ignored: Any) -> dict[str, Any]:
    return buy_command(item, amount, target)


def _use(*, item: str, target: Pos | None = None, **_ignored: Any) -> dict[str, Any]:
    return use_command(item, target)


def _drop(*, item: str, **_ignored: Any) -> dict[str, Any]:
    return drop_command(item)


def _accept_task(**_ignored: Any) -> dict[str, Any]:
    return accept_task_command()


def _submit_answer(*, answer: str, **_ignored: Any) -> dict[str, Any]:
    return submit_answer_command(answer)


def _summon_treasure(*, target: Pos, items: Iterable[str], **_ignored: Any) -> dict[str, Any]:
    return summon_treasure_command(target, list(items))


def default_registry() -> ActionRegistry:
    """A registry covering all twelve official actions."""
    registry = ActionRegistry()
    registry.register("move", _move)
    registry.register("collect", _collect)
    registry.register("build", _build)
    registry.register("remove", _remove)
    registry.register("attack", _attack)
    registry.register("sell", _sell)
    registry.register("buy", _buy)
    registry.register("use", _use)
    registry.register("drop", _drop)
    registry.register("acceptTask", _accept_task)
    registry.register("submitAnswer", _submit_answer)
    registry.register("summonTreasure", _summon_treasure)
    return registry


REGISTRY = default_registry()


def build(action: str, **kwargs: Any) -> dict[str, Any] | None:
    """Build one official command through the default registry."""
    return REGISTRY.build(action, **kwargs)


def supports(action: str) -> bool:
    return REGISTRY.planner(action) is not None


__all__ = [
    "ActionError", "ActionSpec", "ActionRegistry", "Planner", "SPECS",
    "ALLOWED_FIELDS", "REGISTRY", "build", "default_registry", "supports", "validate",
]
