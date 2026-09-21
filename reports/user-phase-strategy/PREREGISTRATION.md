# User phase strategy bounded validation

Frozen before any outcomes for this candidate. This runner has not executed games.

- Development cases: seeds **1, 19**, each challenger and defender.
- Holdout: seed **631**, both sides; do not inspect it before the development review.
- Observed-seven-days profile, pressure 1, 1300 round maximum.
- Same failure_profile.py/reward_profile.py/bench_support.py as archived maintenance-v2 comparison: 50% assigned task failures, reward 80 gold, three tasks per point.
- Legacy early-night-tasks, early-economy, repair-supply, construction-trip, wall-sustain and maintenance-v2 flags forced OFF. The candidate strategy JSON is the implementation being compared.
- No physics, task assignment, actions, answer generation, or timing-window change. Added metrics run outside the timed policy call.
- Require a caller-provided full frozen source SHA and clean tracked source. An actual process exit code must be recorded by the launch supervisor; receipt.json alone is not proof of exit.

Inspect survival rounds, terminal HP, score, protocol/ordinary execution failures, per-controller action exclusion and (candidate only) total maximum one attack each round. Report source SHA and actual loaded config identity. Neither this local model nor scores are official/intranet certification.

Trace already stores every before/after role, so gun positions/levels and controller movement remain reconstructible. Added shared_rocket_control, maintenance, bounded persisted maintenance state, config identity and independent action-exclusion errors. phase-metrics.json reports nightly operator inside/outside and blocked-post rounds, actual successful fire from adjacency common to all three rockets, repair receipts/stock deltas, and purchases.

Launch only once the main reviewer supplies the frozen source/clone:

```text
python evaluate_issue32.py --source <clean-linux-clone> --expected-sha <40-char-sha> --output <new-evidence-directory> --candidate off --early-economy off --repair-supply off --construction-trip off --wall-sustain off --task-gold 80 --tasks-per-point 3 --failure-rate 0.5 --suite training --seeds 1,19 --sides challenger,defender
```

For the registered holdout, change only suite to holdout, seeds to 631, and output directory. Do not run it before authorization after development results.
