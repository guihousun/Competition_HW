"""Independent tests for :class:`agent.tasks.SolverRegistry` priority ordering (P0a).

The registry accepted a ``priority`` argument but sorted by the priority of the
*current* call, so entries actually stayed in insertion order. The default
registration order happened to be ascending, which is why the defect never showed
on the default path; any other insert order silently voided the ordering promise.

These tests use independent sentinel solvers (never the built-ins) and pin the
stored per-entry priority, stable equal-priority order, replace positioning, the
public register/unregister/names/solve contract and the unchanged default order.

    python -m unittest discover -s tests -p test_solver_registry.py -v
"""
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import tasks  # noqa: E402


def context():
    cycle = tasks.TaskCycle(point={"x": 1, "y": 1}, accepted_round=1)
    return tasks.SolverContext(cycle=cycle, round_no=2, phase_task="",
                               llm_resp="", cmd_output="", cmd_status="empty",
                               notes={}, backpack=(), is_day=True)


def plan(kind="submit"):
    return tasks.Plan(kind=kind, answer="a=1")


class Sentinel:
    """Records that it was called and returns a fixed plan or ``None``."""

    def __init__(self, name, result, calls):
        self.name = name
        self.result = result
        self.calls = calls

    def __call__(self, _context):
        self.calls.append(self.name)
        return self.result


class PriorityOrderingTests(unittest.TestCase):
    def test_higher_priority_runs_first_regardless_of_registration_order(self):
        calls = []
        registry = tasks.SolverRegistry()
        # Registered worst-first: a correct registry still runs priority 10 first.
        registry.register("slow", Sentinel("slow", plan(), calls), priority=90)
        registry.register("fast", Sentinel("fast", plan(), calls), priority=10)
        registry.register("middle", Sentinel("middle", plan(), calls), priority=50)
        self.assertEqual(registry.names(), ("fast", "middle", "slow"))
        solved = registry.solve(context())
        self.assertEqual(solved[0], "fast")
        self.assertEqual(calls, ["fast"], "lower-priority solvers must not run after a winner")

    def test_none_falls_through_and_only_the_winner_returns_a_plan(self):
        calls = []
        registry = tasks.SolverRegistry()
        registry.register("give-up", Sentinel("give-up", None, calls), priority=10)
        registry.register("answer", Sentinel("answer", plan(), calls), priority=20)
        registry.register("never", Sentinel("never", plan("submit"), calls), priority=30)
        solved = registry.solve(context())
        self.assertEqual(solved[0], "answer")
        self.assertEqual(solved[1].answer, "a=1")
        self.assertEqual(calls, ["give-up", "answer"],
                         "a None solver falls through; the one after the winner is not called")

    def test_equal_priority_keeps_stable_registration_order(self):
        calls = []
        registry = tasks.SolverRegistry()
        for name in ("a", "b", "c"):
            registry.register(name, Sentinel(name, plan(), calls), priority=50)
        self.assertEqual(registry.names(), ("a", "b", "c"))
        self.assertEqual(registry.solve(context())[0], "a")
        self.assertEqual(calls, ["a"])

    def test_replace_takes_a_new_position_among_equal_priorities(self):
        calls = []
        registry = tasks.SolverRegistry()
        registry.register("a", Sentinel("a", plan(), calls), priority=50)
        registry.register("b", Sentinel("b", plan(), calls), priority=50)
        self.assertEqual(registry.names(), ("a", "b"))
        registry.register("a", Sentinel("a", plan(), calls), priority=50, replace=True)
        self.assertEqual(registry.names(), ("b", "a"),
                         "replace removes and re-appends, so it moves behind its peers")

    def test_replace_changes_the_effective_priority(self):
        calls = []
        registry = tasks.SolverRegistry()
        registry.register("a", Sentinel("a", plan(), calls), priority=50)
        registry.register("b", Sentinel("b", plan(), calls), priority=10)
        self.assertEqual(registry.names(), ("b", "a"))
        self.assertEqual(registry.solve(context())[0], "b")
        # Re-registering "a" with a better priority must move it to the front and
        # make it the chosen solver, not merely change a stored number.
        calls.clear()
        registry.register("a", Sentinel("a", plan(), calls), priority=5, replace=True)
        self.assertEqual(registry.names(), ("a", "b"))
        self.assertEqual(registry.solve(context())[0], "a")
        self.assertEqual(calls, ["a"])

    def test_unregister_removes_the_entry_and_solve_skips_it(self):
        calls = []
        registry = tasks.SolverRegistry()
        registry.register("first", Sentinel("first", plan(), calls), priority=1)
        registry.register("second", Sentinel("second", plan(), calls), priority=2)
        registry.unregister("first")
        self.assertEqual(registry.names(), ("second",))
        self.assertEqual(registry.solve(context())[0], "second")
        self.assertEqual(calls, ["second"])
        registry.unregister("missing")          # idempotent, like the old contract
        self.assertEqual(registry.names(), ("second",))

    def test_duplicate_name_is_refused_and_leaves_the_registry_unchanged(self):
        calls = []
        registry = tasks.SolverRegistry()
        registry.register("a", Sentinel("a", plan(), calls), priority=10)
        with self.assertRaises(tasks.TaskError):
            registry.register("a", Sentinel("other", plan(), calls), priority=1)
        self.assertEqual(registry.names(), ("a",))
        # The refused duplicate must neither replace the solver nor its priority.
        self.assertEqual(registry.solve(context())[0], "a")
        self.assertEqual(calls, ["a"])

    def test_default_priority_is_one_hundred(self):
        calls = []
        registry = tasks.SolverRegistry()
        registry.register("explicit", Sentinel("explicit", plan(), calls), priority=200)
        registry.register("implicit", Sentinel("implicit", plan(), calls))
        self.assertEqual(registry.names(), ("implicit", "explicit"),
                         "an omitted priority must be 100, ahead of 200")
        self.assertEqual(registry.solve(context())[0], "implicit")
        self.assertEqual(calls, ["implicit"])

    def test_empty_registry_returns_none(self):
        self.assertIsNone(tasks.SolverRegistry().solve(context()))

    def test_non_callable_and_nameless_are_refused(self):
        registry = tasks.SolverRegistry()
        with self.assertRaises(tasks.TaskError):
            registry.register("bad", "not callable")            # type: ignore[arg-type]
        with self.assertRaises(tasks.TaskError):
            registry.register("", lambda _ctx: None)
        self.assertEqual(registry.names(), ())

    def test_concurrent_registration_keeps_every_entry_once(self):
        registry = tasks.SolverRegistry()
        errors = []

        def worker(index):
            try:
                registry.register(f"s{index}", Sentinel(f"s{index}", None, []),
                                  priority=index)
            except Exception as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(registry.names(), tuple(f"s{index}" for index in range(8)))


class DefaultRegistryTests(unittest.TestCase):
    def test_opt_in_agent_runs_before_legacy_llm_and_after_keyword_solver(self):
        self.assertEqual(tasks.default_registry().names(),
                         ("keyword-fill", "task-agent", "llm-ask", "probe-command"))

    def test_default_registry_still_solves_keyword_then_llm(self):
        registry = tasks.default_registry()
        stated = tasks.TaskCycle(point={"x": 1, "y": 1}, accepted_round=1,
                                 description="端口：8080", timeout_rounds=20)
        keyword = tasks.SolverContext(cycle=stated, round_no=2, phase_task="端口：8080",
                                      llm_resp="", cmd_output="", cmd_status="empty",
                                      notes={}, backpack=(), is_day=True)
        self.assertEqual(registry.solve(keyword)[0], "keyword-fill")
        question = tasks.TaskCycle(point={"x": 1, "y": 1}, accepted_round=1,
                                   description="请查询北京天气?", timeout_rounds=20)
        lookup = tasks.SolverContext(cycle=question, round_no=2, phase_task="请查询北京天气?",
                                     llm_resp="", cmd_output="", cmd_status="empty",
                                     notes={}, backpack=(), is_day=True)
        self.assertEqual(registry.solve(lookup)[0], "llm-ask")

    def test_module_level_registry_extension_point_stays_empty(self):
        # The module-level REGISTRY is an extension point for plugins; the default
        # solvers live in default_registry(), so fixing ordering must not add them.
        self.assertEqual(tasks.REGISTRY.names(), ())


if __name__ == "__main__":
    unittest.main()
