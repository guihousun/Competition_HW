# Frontline upgrades and navigation validation preregistration

Registered before running this candidate (2026-09-21). No games executed by this preparation task.

- Training: seeds 1 and19, each challenger/defender. These are previously seen development maps.
- Fresh holdout: seed911, challenger/defender. Seed631 is already seen and is not a fresh holdout.
- Do not inspect seed911 until the training review is complete. Freeze the same candidate SHA before training and holdout. Any subsequent tuning requires a newly registered holdout.
- Profile observed-seven-days, pressure1, limit1300; existing local robot/map assumptions unchanged.
- Finite task fixtures unchanged: three per point, 80gold reward, 50% assigned failure, same failure/reward/bench modules byte-for-byte. This is an injected local test condition, not measured LLM success or official task frequency.
- All six historical experimental flags remainOFF. The candidate's actual strategy.json is loaded and its identity/hash reported.
- Full40character expectedSHA and clean tracked source required. Record actualprocess exit separately; receipt.json is not proof of process exit.

Primary checks: official-shape actionaudit, zero duplicate controller action, at mostone attack per round, legal removal by adjacentlivingworker in daytime against ownwall, never automaticremoval of frontfour or twofrontcorners. Independent frontline_metrics computes the sixcells directly from base+publicmap geometry instead of calling candidate frontline.protected_walls. This adopts the existing radius2 local geometry assumption and is not an official building-zone claim.

Additional read-only metrics: traffic reasons, removal receipts/errors, firstafter-round with3livinglevel3guns, gunIDs/types/positions/levels at both daylightend and nightend fordays1–4, successful weaponbuild and quoted purchase spending by weaponupgrade/wallupgrade/wallrepair/othercategory. Missing prices are recordedunknown, notguessed; existing25gold weaponconstruction constant comesfromR03. All audits runoutside timedpolicy computation. Day4goal means level333 firstobserved no laterthan afterround460 (endof4thdaylight; upgrades requiredaytime).

Evaluate survival, score, terminalHP and tradeoffs; report missed333goal honestly. Passing localgames never constitutes official/intranetPASS. No source/package changes are made by this runner.

Command (launch onlyafter reviewer provides cleanfrozen sourceSHA):

```text
python evaluate_issue32.py --source <clean-source> --expected-sha <full40SHA> --output <new-output> --candidate off --early-economy off --repair-supply off --construction-trip off --wall-sustain off --task-gold 80 --tasks-per-point 3 --failure-rate 0.5 --suite training --seeds 1,19 --sides challenger,defender
```

Holdout: change only --suite toholdout, --seeds to911 andoutputdirectory after trainingreview. No physics/fixture changes; benchmark metrics do not influencebrain output.
