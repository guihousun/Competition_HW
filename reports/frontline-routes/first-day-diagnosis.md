# First-day conflict audit (read-only source)

Source: `37b73f357d0771a121711d9bb3eeda4fa739dbb5`; Python 3.11.10; seed 1 defender; observed-seven-days local scenario. Historical four-case audit source c0743915a5ca4b470259a4b7fa9f40fed9b94774. No official environment was tested. No runtime or test source modified.

## Concrete diagnosis

Across the first 70 rounds of historical seeds 1/19 both sides, all 560 worker-round action receipts contain **zero execution failures**. Counts of worker A-B-A position returns with subsequent movement: 1, 7, 1, 2 respectively. A return alone is not necessarily a bug; the seed1 defender r50–52 repeated departure is reproducible and erroneous.

Worker 20012 tries to mine stone (25,13):

| round | observed position | _step_toward proposal | actual final command |
|---|---|---|---|
| 50 | (34,4) | (35,5) | (35,5), succeeds |
| 51 | (35,5) | (34,6) | (34,4), succeeds |
| 52 | (34,4) | (35,5) | (35,5), succeeds |

At r51, `rocket_post.reserve` forbids the common firing post (34,6) for the spare worker. It overwrites a valid mining journey with a move toward any other INNER cell, here (34,4), with no preservation of business destination. The next round's mining planner retries. The dedicated owner 20010 is far away mining (38,12), and does not physically collide with 20012 in this example. The visual apparent fight is a conflict between two planning layers, not the judge rejecting simultaneous worker moves.

Independent eight-neighbour public BFS proves a **legal equally short 11-step path** to a mining stand exists while avoiding the common post:
(35,5) → (35,6) → (35,7) → (34,8) → (33,9) → (32,9) → (31,9) → (30,9) → (29,9) → (28,10) → (27,11) → (26,12).
Thus no wall demolition is needed for this case. Expected first step is a route-progressing cell such as (35,6), not compulsory exact BFS tie-break beyond preserving valid progress. See r51-expected-route.json.

## Additional structural risks

- `_step_toward` sorts interaction stands by Chebyshev distance and picks the first reachable stand, not minimum whole route length; this can switch stand targets and waste movement. Use a multi-goal BFS with actual occupied cells plus reservations blocked.
- `_worker_day` removes a claimed wall target from this round's list but generally has no persistent target lease, so two active builders can exchange assignments from round to round. Current trace does not prove this is the specific reported company incident. Keep a short observation-validated target ownership lease; ensure an actual build or target invalidation releases it.
- `coordination.reconcile` prevents same destination, occupied destinations and swaps; idle-worker yielding does not repair an actively overwritten business movement. More final-pass collision rejection cannot cure the r51 loop.
- `brain._guard_access_repair` is the only runtime automatic `remove` producer found. There is no `_try_escape` in this source. `_guard_access_repair` currently considers adjacent own walls without a protected-front-six filter. Put protection both there and at final proposed-command arbitration so future producers cannot bypass it.

## Independent safety acceptance

1. Four front face walls and two front corners derive from current base geometry/attack side for either team; never a hard-coded blue coordinate list. Those six may not be automatically demolished.
2. Automatic removal only targets a live own wall, by a living worker at true Chebyshev adjacency, during legal daytime, one action per role. No hypothetical simultaneous move-then-remove.
3. Prefer a demonstrably legal nondestructive route or yield. When a wall is genuinely the barrier, prove it connects the affected role to the actual business/service destination, not merely grows an unrelated reachable component.
4. Never assume enemy, robot or other worker future movement. Follow-up after yield/removal must re-evaluate the next real public observation and receipt. No refunds credited for demolition.
5. Avoid demolish/rebuild loops: released corridor cannot immediately return as an obligatory missing wall, unless intentionally closing for defense with an observed reachable return route.
6. Preserve active task holds, voucher use and firing ownership; avoid replacing their commands with generic movement. Route repair must not make a deadline-constrained return impossible.
7. Reproduce r50–52 from public fixtures. Require accepted movement without A-B-A when terrain/goal remain unchanged, and without touching front-six walls. Check no duplicate build/move landings and no swaps in the final commands.
8. Do not promise all conflicts solvable: no legal route, protected barriers or hard deadlines may legitimately require hold/reassignment. Report reason and progress rather than oscillate.

## Evidence files

- analyze.py and day1-analysis.json: historical four-case summary.
- reproduce.py: bounded 70-round reproduction; task fixtures finite 3 per point, 50% injected failures, 80 gold; fixtures affect only simulator, not public policy inputs.
- repro-metadata.json: source commit, Python version, full agent file hashes.
- r46-public.json through r55-public.json: full official-shaped public observations, no _demo.
- corresponding r*-memory.json: actual planner memory before respond.
- repro-trace.json: instrumented step intent, candidate stand path costs, final commands and receipts.
- route-check.py and r51-expected-route.json: independent legal public route avoiding common firing post.

## Follow-up: quarry-return test failure on integrated candidate

Reproduced `TeamTripTests.test_real_quarry_wall_return_revalidates_every_move` on dirty frontline-routes and baseline37b. Candidate test FAIL, baseline PASS. Instrumentation is quarry-repro.py; candidate trace quarry-current.json; baseline trace quarry-Code_HW.json. Runtime files remained untouched.

Candidate actually finishes the return earlier:
- r419 before: worker12 (9,24), construction phase work with2stone. Planner finds no missing contract walls and commands move(9,23), phase return. Receipt succeeds.
- r420 observation: worker12 (9,23), inside shelter and adjacent railgun(10,23). `TripFrame.__init__` cancels the contract with `returned_observed`. This is a valid observed completion.
- The same r420 planning pass assigns ordinary wall building work to now-free worker12, moving to(8,22). Test checks position only AFTER this next ordinary action, misses the safe completion that already happened, and eventually fails.
- Baseline returns/releases r422 with0stone, emitsno ordinary move and happens to stay near the gun, passing the after-settlement assertion.

Candidate traffic events are empty throughout the55rounds, with no demolition/recovery. Different legal route choices/collection prefixes cause6stones instead ofbaseline3on firsttrip and a different completion round; the asserted lease lifecycle itself remains correct.

Suggested precise test update: at the point construction memory is released, check the **current pre-action observation** ofworker12 is inside+gunadjacent, and require the report's `{kind:construction,event:cancel,reason:returned_observed,owner:12}`. Still verify all output moves and actual receipts as before. Add no-release-while-outside if desired. Requiring a gratuitous extra idle turn would change strategy solely to satisfy an observation-timing accident and is unnecessary for the stated finite-trip guarantee.
