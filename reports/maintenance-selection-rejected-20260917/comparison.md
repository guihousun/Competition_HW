# Maintenance selection: four development cases

Candidate `9463590049a8ff8ad9cc1e461eb1eb72455bc142`; verified baseline `9c6ed09adc90f0b2b670456349cfb96c4efad495`; failed historical V2 `fa74d5ab760da601bd644e72962776989ce99303`.

Preregistered development gate: **FAIL**. Total score/minimumHP thresholds were fixed before this run at7141/505.

| Seed/side | Baseline score/HP/rounds | Historical V2 score/HP/rounds | Candidate score/HP/rounds | Candidate errors | Candidate max ms/round |
|---|---:|---:|---:|---:|---:|
| 1/challenger | 1997/1460/1300 | 1618/250/1300 | 1852/1200/1300 | 0 | 332.3/r1197 |
| 1/defender | 1920/505/1300 | 1375/0/1278 | 1689/240/1300 | 0 | 590.0/r551 |
| 19/challenger | 1619/1440/1300 | 1431/1500/1300 | 1583/1500/1300 | 0 | 210.9/r315 |
| 19/defender | 1605/1260/1300 | 1417/1240/1300 | 1399/1340/1300 | 0 | 320.2/r575 |

Total scores: baseline7141, historical V25841, candidate6523. Minimum final baseHP: {'baseline': 505, 'historical_v2': 0, 'candidate': 240}.

| Gate | Result |
|---|---|
| no_earlier_base_destruction | True |
| no_added_protocol_execution_errors | True |
| total_score_at_least_7141 | False |
| minimum_hp_at_least_505 | False |

## New dispatches and budget

| Seed/side | Actual new purchase sequence | Upgrade spend | Repair spend | Gold left | Confirmed upgrades | Repair uses day/night | Observed return day/night |
|---|---|---:|---:|---:|---:|---:|---:|
| 1/challenger | r261:R, r391:U, r521:R, r651:U, r683:R, r801:U, r931:R, r1061:R, r1191:R | 300 | 60 | 0 | 3 | 6/0 | 6/0 |
| 1/defender | r261:R, r391:U, r423:U, r541:R, r671:U, r801:R, r1171:R | 300 | 40 | 49 | 3 | 4/0 | 4/0 |
| 19/challenger | r261:R, r391:U, r399:R, r521:U, r651:R, r781:U, r911:R | 200 | 40 | 0 | 2 | 4/0 | 4/0 |
| 19/defender | r261:R, r391:R, r521:U, r671:R, r781:R, r911:R, r1061:R, r1171:R | 100 | 70 | 70 | 1 | 7/0 | 7/0 |

R=repair, U=upgrade. Sequence records a real opportunity, not successful use. Full selection feasibility/prior-history records, accepted trades/use, return events, unused vouchers and slowest request/planner context are retained in comparison.json and case folders.

- Four already observed development cases. Baseline and failed V2 reused, not rerun. No heldout claim.
- Identical physics, initialization, failure assignment, runtime and full trace-only runner. Baseline ENV OFF; both V2 variants ON.
- Candidate timings measured later with different background load, not an official response-time guarantee.
- Selection diagnostic describes proposals; dispatch sequence counts only final TripFrame issued events.
- No tuning, seed expansion or default enablement from these results alone.
