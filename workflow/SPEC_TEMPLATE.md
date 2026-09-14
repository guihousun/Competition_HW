# Issue #<number> / Spec v<revision>

- Issue URL:
- Owner input digest / comment IDs:
- Tested SHA / target base SHA:
- Category: official-fix | strategy | tooling | documentation
- Rules: R01–R08 and original chapter
- Risk / needs intranet retest: yes/no (default yes for behavior)
- Approved by: Codex
- Approval time / Spec SHA256:

## Evidence and problem
Observed behavior, source version, minimal reproduction. Separate known facts from hypotheses.

## Goal and non-goals
One bounded change. Do not alter official source documents or unrelated work.

## Design
Modules, data flow, compatibility, failure handling. List allowed file paths and prohibited paths.

## Acceptance criteria
Independent expected outcomes. Include a previously failing example where applicable.

## Executor instructions
Read project contracts. Implement only this Spec in the assigned worktree. Return file list,
test commands/results, outstanding risks. No commit/push/merge or GitHub communication.

## Local verification
Focused tests; protocol/strategy/settlement changes require full unittest discovery;
strategy changes need representative both-side and held-out-seed checks. Always diff --check.

## Intranet retest instructions
Exact candidate SHA, how to run, expected result, acceptance cases and rollback SHA.
Owner reports PASS/FAIL and the same SHA. Local simulation cannot replace this evidence.

## Review and merge record
Reviewer, reviewed head SHA, test/check evidence, owner PASS comment if required,
remaining gaps, merge method and merge SHA. Any code change invalidates prior approval.
