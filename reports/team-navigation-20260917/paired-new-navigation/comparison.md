# Issue32 team-trip: frozen four-development-pair comparison

Baseline `63f24d6797038b25299abcc20358aa84d695078b`; candidate `1b8da86aaf83e71904b4a6b213cac3634781d051`.

## Preregistered gate

Development gate: **PASS**. Per-pair survival/error results are retained in comparison.json.
Total score 9069 -> 9420 (+351). This is a small development-set gain, not broad improvement evidence.

| Seed | Side | Baseline score | Candidate score | Delta |
|---|---|---:|---:|---:|
| 1 | challenger | 2097 | 2130 | +33 |
| 1 | defender | 2501 | 2415 | -86 |
| 19 | challenger | 2417 | 2429 | +12 |
| 19 | defender | 2054 | 2446 | +392 |

## Survival and worst-response context

| Seed/side | Rounds baseline -> candidate | Base HP baseline -> candidate | Max ms baseline -> candidate | Candidate max round |
|---|---:|---:|---:|---:|
| 1/challenger | 1300 -> 1300 | 1100 -> 1330 | 497.2 -> 1022.6 | 142 |
| 1/defender | 1300 -> 1300 | 940 -> 750 | 329.1 -> 1273.7 | 45 |
| 19/challenger | 1300 -> 1300 | 1300 -> 1410 | 304.5 -> 362.6 | 791 |
| 19/defender | 1300 -> 1300 | 1195 -> 740 | 352.1 -> 1585.6 | 139 |

All eight matches completed1300 rounds with no execution or audit errors. Candidate base HP is lower in both defender cases; no claim of universally stronger defense follows from the score gain.
Each case folder includes `slowest-request.json`, `slowest-planner-before.json`, and `slowest-context.json`. The maximum round, duration, response and request observation hash are independently checked against the per-round trace. Timing excludes snapshot/serialization; local concurrent load remains a limitation.

## Recorded economy and construction

Each cell is baseline -> candidate. Purchases use the actual quoted price and feedback-confirmed quantity. Upgrade counts additionally require an observed target level increase.

| Seed/side | Spend | Sell income | Final gold | Upgrades | Wall builds | Final walls | No-command worker rounds |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1/challenger | 480 -> 480 | 0 -> 50 | 0 -> 50 | 4 -> 4 | 25 -> 27 | 16 -> 18 | 1486 -> 1225 |
| 1/defender | 630 -> 630 | 50 -> 46 | 50 -> 46 | 5 -> 5 | 31 -> 31 | 16 -> 15 | 1169 -> 1261 |
| 19/challenger | 630 -> 630 | 0 -> 0 | 0 -> 0 | 5 -> 5 | 22 -> 24 | 16 -> 15 | 1474 -> 1423 |
| 19/defender | 480 -> 630 | 50 -> 30 | 50 -> 30 | 4 -> 5 | 30 -> 31 | 15 -> 16 | 1213 -> 1335 |

Full trade/upgrade rounds, night-start wall HP/levels, unused vouchers, and trip events are in `comparison.json`.

## Reproducibility and limits

Both arm subprocesses exited0 with unchanged frozen sources. Every pair has equal initial-state hash and failure assignment. Runner, helpers, parameters, and physics/initialization modules match. Candidate also changes diagnostics/console_digest and adds robot_occupancy; these are recorded diagnostic differences, not falsely called byte-identical. No simulator/taskworld differences.
Local max policy response 497.2 -> 1585.6 ms. This increased substantially; runs were concurrent, and this is not a platform timing guarantee.

- Four previously seen development cases only; not heldout or official PASS.
- Failure probability zero, gold80 local scripted task fixture; six finite tasks per match.
- Two Windows3.11.10 arms ran concurrently. Latency is local measurement, not platform guarantee.
- Candidate functional full-suite/package validation is recorded separately by parent; this report only covers four paired matches.
- No-command worker rounds include valid waiting/cooldown, not proven lost work.
- Upgrade/wall/budget contrasts are descriptive, not causal attribution.
- Earlier self-blocked-navigation fitness is excluded. Initial baseline attempt exited1 on Windows status-file rename at round440; only its unchanged-source retry is compared.
