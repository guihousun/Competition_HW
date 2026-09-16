# Issue32: identical-decision route-planning performance

## Scope and rules

Engineering optimization of the strategy from
`e9ab8e871cc7382c3cf0b327d798d989065b33d9`, not a simulator or game-rule change.
R01 (interface time budget; task book §8), R02 (ordered eight-direction legal
movement; task book §4.1–4.3) remain unchanged. No budget, target priority,
tie-break, route overlay cap64, deadline, observation or role permission changes.

The preceding four-pair development evaluation met its numerical gate, but a
subsequent simulator navigation deadlock audit invalidated strong defensive
fitness conclusions. Its 1300-round survival must not be described as robust
defensive improvement. This change uses only recorded public input history to
prove equivalent decisions; no new fitness claim follows.

## Evidence and smallest change

A real-memory replay of seed1/defender public rounds1–165 reproduces every
recorded response and upgrade report. At round139, cProfile identifies58 calls
to `team_trip.evaluate_trip`, with roughly1.99million neighbour-generator calls
and4.52million position hashes. Profile overhead is substantial; profiled time
must not be presented as ordinary response latency.

Both route modules repeatedly allocate the same eight `Pos` neighbours in BFS
overlays. `Pos` is a frozen dataclass. Cache only this coordinate geometry in
each module's bounded `lru_cache(maxsize=4096)`, returning an immutable tuple
in precisely the previous dx/dy order. Do not cache generators, passability,
dynamic occupancy, costs, decisions or match state. Eviction changes only work
performed, not results. Separate existing functions avoid a cross-module
refactor and retain callers and iteration order.

## Acceptance

- Geometry tests check exact direction order, reusable traversal, immutable
  values, bounded size and eviction invariance.
- Relevant construction and trip-contract tests, including failed receipts,
  fixed first actions, full return budgets and both teams, must pass.
- Reconstruct real planner memory by feeding the complete165 public inputs in
  sequence. Demand strict equality of the full response, decision report and
  planner dump at each round against e9. Also check recorded response and
  upgrade-report equality. Do not initialize isolated hotspot memory by guess.
- Compare unprofiled runs on the same Python3.11.10 host sequentially; record
  order, cold-cache process boundaries, timing distributions and uncontrolled
  background-load limitations. Profile each arm separately for attribution.
- Freeze the minimal source commit and run the full suite. Engine/taskworld
  hashes must still match e9. No full matches or extra heldout seeds required
  for this equivalence-only performance patch.

Local evidence lives under
`.workflow/issue32/team-validation-e9ab8e8/`; persistent receipts, replay script,
profiles, raw decisions and timing arrays preserve the exact scope. These are
local measurements, not an official platform latency or PASS guarantee.
