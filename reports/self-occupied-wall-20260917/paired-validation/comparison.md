# Self-occupied wall: bounded validation

Baseline `1b8da86aaf83e71904b4a6b213cac3634781d051` -> candidate `2296d9a16aa9a96df41225a785f0b313e12463f2`.

Preregistered safety/error gate: **PASS**.

| Seed/side | Task failure | Rounds | Score | Base HP | Execution errors | Candidate slowest ms/round |
|---|---:|---:|---:|---:|---:|---:|
| 19/challenger | 0.0 | 1300 -> 1300 | 2429 -> 2429 | 1410 -> 1410 | 0 -> 0 | 391.1/r791 |
| 19/defender | 0.0 | 1300 -> 1300 | 2446 -> 2485 | 740 -> 1140 | 0 -> 0 | 1506.0/r139 |
| 127031/challenger | 0.5 | 1300 -> 1300 | 2027 -> 2027 | 1100 -> 1100 | 0 -> 0 | 332.2/r139 |
| 127031/defender | 0.5 | 1300 -> 1300 | 2069 -> 2069 | 1500 -> 1500 | 0 -> 0 | 482.9/r408 |

Every pair has matching initial-state hash and task failure assignment. All source receipts contain actual exit0 and unchanged frozen sources. Full public slowest request, planner-before and response are saved per case and independently hash-checked.

| Seed/side | Purchases spent | Confirmed upgrades | Walls built | No-command worker rounds |
|---|---:|---:|---:|---:|
| 19/challenger | 630 -> 630 | 5 -> 5 | 24 -> 24 | 1423 -> 1387 |
| 19/defender | 630 -> 630 | 5 -> 5 | 31 -> 31 | 1335 -> 1333 |
| 127031/challenger | 330 -> 330 | 3 -> 3 | 29 -> 29 | 1513 -> 1513 |
| 127031/defender | 330 -> 330 | 3 -> 3 | 28 -> 28 | 1342 -> 1342 |

## Observed action-chain validation

Seed19 defender first differs at r1042 with identical public input: candidate adds worker20012 move(33,5), r1043 builds(32,4) with successful feedback, r1044 records returned_observed and releases the construction commitment. This new full run confirms the intended chain with actual evolving planner state; it is not a fabricated recovery of the earlier missing planner dump.
Seed19 challenger has38 response-difference rounds starting r817 despite unchanged score/HP. Do not call its trajectory identical. Both locked seed127031 sides have zero response differences and identical before/after public hashes over all1300 rounds; this sample does not exercise a different outcome and cannot establish broad safety.

No-command worker rounds include valid waiting, not proven wasted work. Functional tests use the exact historical public r1042 request with explicitly empty PlannerState; this match evaluation is a new complete execution from the same initialized case, not a claim to reconstruct the lost historical memory.

- Exactly six new matches; two historical seed19 baseline matches reused only with matching engine/runner/initial hash.
- Seed127031 locked before inspecting results; failure0.5 is synthetic task-timeout stress, not official measured LLM quality.
- Parallel arm execution and other host load limit timing comparisons; reused baseline measured earlier.
- No strategy adjustment after seeing results; no additional seeds or full matrix.
