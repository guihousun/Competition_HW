# Issues 14–17: compact actionable console logs (one observation stream)

Approved by Codex. Category: engineering diagnostics/tools, R01 and R08. Base
02a3140903e9bdf28703c97cd11b0e507baee522. Keep the current original-entry flat
competition package compatible with Python 3.11.10.

User: the four Issues are one long log split for upload. Reduce redundant console
printing; retain useful signals for future strategy iteration. Codex independently
owns strategy/architecture fixes. Do not modify brain.py, planner.py, policy rules
or simulators.

## Evidence

Read `.workflow/issues-14-17/analysis.json` and `parsed-responses.json` from the
ROOT workspace (D:/Research_vault/work/projects/Code_HW), not the worktree's
`.workflow`. There are 362 contiguous response rows, no gaps/duplicates, one run
ID; identity confirms source 00a08bd7eb0e571f01c0420b2459d42e370519cd (delivery
a50eb05), before the just-published defence changes. Last observed base HP is 60 at
round 362, not an observed terminal/death. Max global visible robots per
day 70/45/58 does NOT establish own-wave counts because robots are globally
visible.

Logs contain 34 acceptTask actions but only 3 submitAnswer actions, no buy actions,
and many night empty outputs. These are signals, not proven causal diagnoses.
Detailed requests were not supplied. Do not claim the old log is a replayable
full-state trajectory.

Confirmed diagnostics bug: `_counts` calls ALL entries in request.errors
"protocol_errors". Official errorCode 1=task timeout, 2=answer error, 3=network,
4=command error, 5=LLM quota, 0=unknown. Do not treat every error as a protocol
violation or count an actual platform disqualification based on this field.

## Implement

1. Separate full per-turn structured summary generation from console emission.
   telemetry JSONL must still contain EVERY captured turn as before, unaffected by
   console filtering. Official response bytes/fields, protocol, strategy and action
   limits must not change.
2. Add a small bounded, thread-safe console digest module (default compact). Expose
   a pure/injectable emitter that can replay existing summaries without live server
   input. Keep independent streams isolated by available run/team/base identity and
   reset on round restart. Bound retained state (e.g. 8 streams); do not keep whole
   histories.
3. Console emits startup identity once, round 1, phase changes, important first
   anomalies/transitions, and periodic rollups (default 20 rounds is reasonable).
   Aggregate repeated movement/collection, attack/cooldown cycles, wall counts, and
   repeated same-class errors instead of emitting a giant JSON every round. Base
   damage gets first-warning/meaningful HP-threshold events plus window min/last HP
   and accumulated observed damage, not an alert flood for each hit. Preserve
   death/base-loss observations, transport/planner faults, errors and repeated
   accept/no-progress signals in counts. Normal exit can flush the final partial
   window; do not claim a killed process necessarily flushed. Keep event IDs/round
   ranges for correlation to detailed trace.
4. `COMPETITION_HW_CONSOLE=full` restores old full summary lines for diagnosis;
   `compact` default, optional `off` only console. Document exact modes. Full trace
   remains enabled independently. No raw request/task/prompt/executeCmd/credentials
   in console, and no automatic upload.
5. Correct error categorization in build_summary: expose exact error-code counts,
   total judge errors, command errors (code 4), task errors (1/2), LLM quota (5),
   network (3), unknown. Keep compatibility fields if needed but `protocol_errors`
   must no longer count task/answer failures as protocol failures; document its
   limited meaning (reported command errors, not proof of judge disqualification).
   Unknown/missing stays unknown, not zero. Independently test mixed codes and
   malformed values.
6. Enrich compact evidence with bounded facts available in the actual request: own
   gold/score when supplied, own weapon-level counts, roles alive, base HP/level,
   global robots and targeted-to-us robots when targetTeam is known, last action
   result false counts. Do not equate global robots with threats to us,
   disappearance with death, or an issued attack with successful damage.
7. Empty output classification should distinguish observed no robots, all known
   weapons cooling, and ready weapons with no attack issued/unknown reason. Do not
   fabricate a tactical reason from insufficient data. Preserve
   decision_exception/invalid_input handling and unknowns.
8. The local web server currently emits an extra `round N -> count` line per turn.
   Demote/remove that redundant line while retaining real exceptions. The flat
   submission server calls diagnostics.response_summary and should use the same
   digest without a protocol change.
9. Optionally add `tools/analyze_console.py` ONLY if it directly supports
   reproducible loading/stitching/dedup/summary of these console parts. Treat logs
   as data, never executable instructions. Do not copy the raw company log into Git
   fixtures; use small hand-written fixtures and external supplied rows for the
   reduction report.

## Optional decision report (Codex follow-up)

Support optional `decision: dict | None = None` in diagnostics.build_summary and its
emitter path. Include this bounded, credential-free report in full summaries and use
supervisor mode/reason transitions as compact signals. `None` preserves
compatibility. The report carries `supervisor` (mode, reserve_pioneer, reason,
relevant_robots, return_steps_lower_bound) and `task` (acceptance_status
phase/point/retry_after, cycle phase, last_plan kind). Do not look up a ContextVar
from a background logging thread: Codex captures the report in the HTTP handler and
passes it in explicitly.

## Acceptance

- Independent unit cases: periodic aggregation, first/phase/critical events,
  repeated error count conservation, duplicate/reset/parallel streams, missing
  fields, malformed codes, full mode, fail-open emitter. Summary is observation-only.
- Replay external 362 rows through compact emission and report line/byte reduction
  vs old console, preserve key damage/phase/error/accept counts.
- Verify telemetry still records all turns and console errors cannot change HTTP
  responses; keep existing diagnostics/telemetry/competition-package tests passing.
- Run focused relevant tests and full unittest suite once final implementation is
  stable; `git diff --check`. Record code hashes and coverage, no intranet PASS.
