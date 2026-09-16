# Maintenance v2: four opt-in development pairs

Baseline `9c6ed09adc90f0b2b670456349cfb96c4efad495` OFF; candidate `fa74d5ab760da601bd644e72962776989ce99303` ON.

Preregistered survival/error gate: **FAIL**. This does not enable the feature by default.

| Seed/side | Rounds | Base HP | Score | Execution failures | Candidate worst ms/round |
|---|---:|---:|---:|---:|---:|
| 1/challenger | 1300 -> 1300 | 1460 -> 250 | 1997 -> 1618 | 0 -> 0 | 617.1/r784 |
| 1/defender | 1300 -> 1278 | 505 -> 0 | 1920 -> 1375 | 0 -> 0 | 665.4/r811 |
| 19/challenger | 1300 -> 1300 | 1440 -> 1500 | 1619 -> 1431 | 0 -> 0 | 313.4/r315 |
| 19/defender | 1300 -> 1300 | 1260 -> 1240 | 1605 -> 1417 | 0 -> 0 | 301.1/r311 |

## Budget, maintenance and role ownership

| Seed/side | Gold left | Repair kits bought | Repair uses day/night | Observed return day/night | Confirmed upgrades | Ownership anomalies |
|---|---:|---:|---:|---:|---:|---:|
| 1/challenger | 60 -> 270 | 0 -> 9 | 9/0 | 9/0 | 3 -> 0 | 0 |
| 1/defender | 50 -> 255 | 0 -> 8 | 8/0 | 8/0 | 3 -> 0 | 0 |
| 19/challenger | 40 -> 160 | 0 -> 8 | 8/0 | 8/0 | 2 -> 0 | 0 |
| 19/defender | 40 -> 60 | 0 -> 8 | 8/0 | 8/0 | 2 -> 1 | 0 |

All actual exit receipts, hashes, per-round accepted maintenance commands, bounded ownership records, repair consumption and full slowest public request/planner-before are preserved. comparison.json contains precise rejection/blocked-return and upgrade/trade details. Counts do not assume an absent action is failed ownership.

- Four previously observed development pairs, synthetic50% task failures/gold80/three tasks per point; not heldout.
- Full current physics/initialization/assignment and identical trace-only runner in both arms. No earlier self-blocking results reused.
- Two processes concurrent with host background work; timing is local, not official platform guarantee.
- No further parameter adjustment, seed expansion, or default feature enablement authorized by a numerical pass.
- Repair success counts require accepted use and observed inventory consumption; net wall HP may decrease under concurrent damage.

## Outcome and bounded diagnosis

Total score 7141 -> 5841; every pair loses score. Candidate seed1 defender loses its base at1278 versus baseline survival1300. Retain candidate as an isolated failed experiment; do not enable by default.
All33 candidate repair kits are consumed in daytime, with observed returns. Weapon upgrade completions collapse from10 to1 while substantial gold remains. This indicates new repair-vs-upgrade proposal choice needs redesign; it is not evidence that an already issued upgrade lease was stolen. See seed1-blue-diagnosis.md/json for exact first-divergence and earlier repair benefits, without a counterfactual causal claim.
