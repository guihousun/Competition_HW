# One-run feedback architecture iteration (Issues 14–17)

Owner explicitly confirms four Issues are parts of one log and requests compact important logs plus strategy architecture improvements. Base: current codex/sgh 02a3140903e9bdf28703c97cd11b0e507baee522. Logged source is 00a08bd7eb0e571f01c0420b2459d42e370519cd, before the already-delivered defence-layout fix. Do not redispatch that old task or claim this log tested the new defence.

## Observed evidence

362 contiguous round summaries, one identity/run, no duplicates/gaps. Identity verified and Python3.11.10: evidence the old flat package actually started and served this stream on the platform, not proof of a winning/full game. Last observed base HP60; no observed terminal/base death. All visible robots are global, not own-wave counts. No raw requests, positions, gold/score/weapon levels or exact error codes in these console rows.

Actions: acceptTask34, submitAnswer3, sell2, buy0. Base remained level1. Night1 only workers20010/20012 issued attacks while pioneer remained alive; later all3 controllers appear. Base HP ranges day1 1500→1325, day2 1325→900, day3 900→60. Max plan_ms196.1 is planning only, not end-to-end network latency.

Confirmed code-level defects/risks to reproduce independently:
- diagnostics.py labels all request.errors as protocol_errors; task/answer failures must be distinguished.
- TaskPipeline._maybe_accept chooses ready targets but then accepts at any `on_point`, even an unready current point. If all published points are cooling but valid, it falls back to map zones and can accept a cooling point. Published per-point readiness must be authoritative.
- brain.plan_for_state unconditionally lets a task claim override an existing defence claim, including dusk/critical base danger. task waiting may remove a pioneer defence move/attack. The user objective puts base survival before score; explicit risk-based arbitration is needed.
- An unconfirmed newly accepted task has blank description/timeout0. Solver/deadline logic must not treat missing data as an immediate expired task and spin accept every two rounds. Need an acceptance-observation phase and bounded retry/backoff; no fabricated official cooldown/deadline.

## Architecture and implementation split

DSH QA native session owns compact console digest and typed diagnostic error classification per logging-spec.md. It must leave per-turn JSONL intact. Codex owns the strategy/task architecture in a separate worktree; no shared live edits.

Codex plan:
1. Extract a small public-observation supervisor/policy module. Make preparation/defence vs task permission explicit, based on official day/night, actual own weapons/controllers, visible relevant threats and base health. No private simulator seed/hidden wave use. Strategy thresholds labelled tunable choices, not official constants. No neural-network/training claim.
2. Guard task, treasure and task-walk overrides consistently when the pioneer is reserved for necessary defence. Preserve existing tasks/solver memory and pending judge result bookkeeping when pausing; never execute LLM/shell outside official channels. New dusk task acceptance must not defeat return-to-defence. Avoid blindly cancelling safe tasks when there is no defence need; preserve role/action/controller exclusivity through existing reconciliation.
3. Make per-point readiness and acceptance confirmation explicit. Accept only at a currently ready own point, respect cooldown and isValid, never borrow another point's readiness. A pending accept with no published task waits briefly; explicit failure or prolonged missing observation produces bounded per-point strategy backoff, not fake task success or invented game cooldown. Adopt published description/timeout before solver planning; unknown timeout remains unknown. Preserve accepted-round identity when filling metadata.
4. Retain existing shared team budget and loadout. No automatic buy/upgrade retuning based solely on this log: gold and actual prices are absent. Add needed observability so future tests can distinguish no money, no route, no useful purchase, and priority blockage.
5. Add a concise architecture/evidence document and reproducible small synthetic regressions. Validate task-vs-defence arbitration, confirmation delay/failure, unready current/ready alternate point, multiple teams, previews without mutation, unchanged official response keys. Run relevant existing tests and representative left/right holdout cases; full suite after integration.

## Delivery

One integrated reviewed release on codex/sgh. Build via tools/build_competition.py with original main3.py entry; actual package tests in Python3.11.10; commit CoreGeek.tar.gz, .sha256 and CoreGeek.manifest.json in root. Do not change official documents/sample/archive or game numerical rules. Report observation scope and uncertainty; no intranet PASS/auto-close. Issue14 is the primary discussion, 15–17 are linked parts of same execution. Keep state.py untouched and mark event processed only after triage/delivery decisions are recorded.

## Reviewed rollout amendment

Healthy-base enforcement is advice-only by default after mixed holdout score outcomes. Damaged-base return/defence enforcement remains active. Add final controller exclusivity and second-point task reconstruction. Do not describe full-risk enforcement or score improvement as validated. Current implementation and final local evidence: docs/POLICY_ARCHITECTURE.md and reports/issue-14-17-architecture/REVIEW.md.
