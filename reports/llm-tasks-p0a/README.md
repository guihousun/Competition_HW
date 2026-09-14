# LLM tasks P0a — deterministic foundations (report)

Small aggregate report. Category: internal scheduling implementation fix + tests
(R01/R07; 接口文档 §1.7, §2.1; 任务书 §5.3). No official value changed.

## Base

| Item | Value |
|---|---|
| Worktree | `.workflow/worktrees/ds-recovery` |
| Base HEAD | `1fcf7ba9cf93add6c2b6684ca4bc62a2acdef850` |
| Python (tests) | `3.11.10` (uv platform interpreter `cpython-3.11.10-windows-x86_64-none`) |

## Defect and fix

`SolverRegistry.register` accepted `priority` but sorted with
`key=lambda item: priority` — a closure over the *current* call's priority, so the
registry actually kept insertion order. It was invisible only because the default
registration order (`keyword-fill` 10, `llm-ask` 50, `probe-command` 80) is already
ascending.

Fix (only the `SolverRegistry` part of `Demo/CoreGeek/src/agent/tasks.py`):
`_solvers` now stores `(name, solver, priority)`; `register` sorts by the **stored**
priority (`key=lambda item: item[2]`). Python's sort is stable, so equal priorities
keep insertion order and `replace=True` (remove + append) takes a new
registration-order position. Public `register` / `unregister` / `names` / `solve`
shapes, duplicate-name refusal, the `RLock` and the default order are unchanged.

## Changed files (SHA256)

| File | SHA256 |
|---|---|
| `Demo/CoreGeek/src/agent/tasks.py` | `a04551df7d25e72fdbf3a947cb8bb367d90aa02858975e6c35a89097afef5b63` |
| `tests/test_solver_registry.py` (new) | `dea9a14b04a6450cac6bcb1b3f92b4c3ae0af6719bb7e73d95c4f7643e9fc33f` |
| `tests/test_llm_budget_contract.py` (new) | `3d850fe58eb03137589f4f3abd7ee0f64c72722b62d5a2fa348e3985e8645582` |

## Tests

| Command | Result |
|---|---|
| `python -m unittest tests.test_solver_registry tests.test_llm_budget_contract` | 26 tests, OK |
| `python -m unittest tests.test_tasks` | 48 tests, OK (unchanged) |
| `python -m unittest discover -s tests -v` (Python 3.11.10) | 513 tests, OK, exit 0 |

Coverage:

* **Registry (14 tests)** — independent sentinel solvers assert *chosen solver and
  call order*, not just stored numbers: higher priority wins from reversed
  registration order; `None` falls through and only one plan is returned with the
  post-winner solver never called; equal priorities stay stable; `replace` moves
  position and can promote a solver to front; unregister/duplicate refusal; default
  priority 100; concurrent registration keeps every entry once; default order and
  solve order (`keyword-fill` → `llm-ask`) unchanged; module-level `REGISTRY`
  extension point stays empty.
* **LLM allowance (6 tests)** — guarded consumption (never a bare `consume_llm`):
  3/4 boundary, in-task calls available and uncounted, in-task work does not disturb
  the normal balance, round 130 → 131 reset, two `PlannerState`s independent, match
  round-restart reset.
* **Planner memory (6 tests)** — `pending_prompt`/`pending_cmd` and counters survive
  dump → JSON → load; current task text, type, point, submission records, phase and
  best rate survive; `solver_notes` survive; a same-round result is not mistaken for
  a new result (and is consumed exactly once one round later); parsed
  `lastCmdResult` survives.

## Default-response parity (bounded smoke)

Method: the current tree was compared with the **HEAD** `tasks.py` loaded from a copy
of the same `src` tree (all other modules identical). For each, one round is planned
from a fresh `PlannerState` (a deliberately memory-free parity probe) for the unchanged
official sample `docs/request.txt` (rounds 1–3) and for 12-round scenario runs on
challenger **and** defender (`scenario(seed, side, 1)`, seeds 3 and 7); a SHA256 is
taken over every produced `roleCommandMap`.

```
current : e9615263f065bfcd3cf119a84cfc16609726a0e54d34d21e361aa23d225058d3
baseline: e9615263f065bfcd3cf119a84cfc16609726a0e54d34d21e361aa23d225058d3
identical: True
```

This is deliberately a short bounded smoke, not a benchmark: Codex owns the
baseline/holdout simulations and the final package checks. Because the default
registration order already matches the priority order, the fix cannot reorder the
default path — the identical hashes are consistent with that.

## Limitations / still open

* P0b (how the allowance is classified once the judge *confirms* a task) is **not**
  addressed here; only the existing primitives were pinned.
* `consume_llm` remains an accounting primitive, not an enforcement gate; the tests
  demonstrate the guard (`llm_available`) is what stops the 4th call.
* No real LLM/network call was made; no official value, archive or other module was
  changed.
* Full suite: **513 tests, OK** under Python 3.11.10 (see
  `full-suite-summary.txt`); no unrelated failure was present in this run.