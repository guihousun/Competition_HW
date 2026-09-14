# Four-part log: reviewed console reduction

Input: Issues 14–17, one run, 362 contiguous summaries, source commit `00a08bd7eb0e571f01c0420b2459d42e370519cd`. No raw requests or official terminal observation. Full source log is kept in the local workflow evidence, not copied into Git.

Final reviewed replay: **362 → 35 lines**, **249384 → 5026 UTF-8 bytes** (about 98.0% fewer bytes). This comparison uses the historical summaries, which do not contain new decision/economy fields; it is not a guarantee that every future run will have exactly 35 lines.

Retained aggregate evidence: 34 acceptTask, 3 submitAnswer, 6 reported errors, 1440 observed HP drop, final observed base HP60. Error codes are absent in the old rows; their categories remain unknown. `reduction.json` verifies count and damage conservation. `compact-console.txt` is the small replay output.

Reproduce: `python tools/analyze_console.py reduce <parsed-responses.json> --dump <output.txt>`.

Review corrections include per-stream labels, exact same-round dedup, flushing pending totals on restart, exact window ranges and cross-window HP deltas, bounded decision fields, and suppressing per-step distance/HP chatter. Periodic new summaries retain gold, score, weapon type/level and issued controller counts; these are observed facts, not proof of attack success.

The original DSH turn completed. Its review followup ended with an explicit provider `PI_AI_ERROR`; Codex reviewed the partial changes and finished the small integration fixes. Original DSH test evidence is historical in `full-suite-summary.txt`; final integrated verification is in [architecture review](../issue-14-17-architecture/REVIEW.md). No official/intranet PASS is claimed.
