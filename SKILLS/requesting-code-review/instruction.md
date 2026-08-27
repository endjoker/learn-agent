# Requesting Code Review

Use a focused code-review Subagent to obtain an independent review of a completed, bounded change. The Subagent receives only the review brief you provide, not the parent conversation transcript.

**Runtime contract:** Call the native `create_subagent` tool only when it is available. JKagent permits one child level only: the reviewer must not create or request another Subagent. The parent remains responsible for interpreting findings, making changes, testing, and reporting the result.

**Core principle:** Review early, review often—but do not delegate an ambiguous or still-moving task.

## When to Request Review

**Recommended:**
- After a completed, independently testable plan task
- After a major feature or risky refactor
- Before merging or handing over a change
- After a complex bug fix, especially where regressions are plausible

**Do not create a Subagent merely to avoid understanding the change.** If the review scope is tiny or the runtime does not expose `create_subagent`, perform the review in the current session using the same checklist.

## Review Workflow

### 1. Stabilize the review target

Before delegation:
1. Finish the bounded change and run its relevant tests.
2. Identify the comparison range and changed files:

```bash
BASE_SHA=$(git rev-parse HEAD~1)  # or origin/main
HEAD_SHA=$(git rev-parse HEAD)
git diff --stat "$BASE_SHA" "$HEAD_SHA"
```

3. Write a concise brief using [code-reviewer.md](code-reviewer.md). Include:
   - `DESCRIPTION`: what changed and why
   - `PLAN_OR_REQUIREMENTS`: expected behavior and constraints
   - `BASE_SHA` / `HEAD_SHA`: review range, if Git history exists
   - exact test commands already run and their outcomes
   - explicit review boundaries (for example, “review only API compatibility and error handling”)

Never ask the reviewer to infer requirements from the parent transcript.

### 2. Delegate one reviewer when appropriate

Use `create_subagent` with the completed brief as its task. Ask for a structured report with:
- strengths;
- findings grouped as Critical, Important, or Minor;
- evidence with file/line references;
- missing or weak tests;
- a clear ready/not-ready assessment.

The reviewer is advisory. It must inspect and report; it must not make unrelated changes, start a new Plan or Goal, or spawn child agents.

### 3. Act on the report

- Fix **Critical** findings before proceeding.
- Fix **Important** findings before handoff unless the user explicitly accepts the risk.
- Record **Minor** findings with a reason if deferred.
- Challenge an incorrect finding with concrete code or test evidence.
- Re-run affected tests after fixes; request a follow-up review only when the change materially alters the reviewed scope.

## Example

```text
A bounded implementation task is complete and its tests pass.

Create one code-review Subagent with this brief:

DESCRIPTION: Added verifyIndex() and repairIndex() for four corruption cases.
PLAN_OR_REQUIREMENTS: Task 2 in docs/plans/deployment-plan.md; preserve existing API behavior.
BASE_SHA: a7981ec
HEAD_SHA: 3df7661
TESTS RUN: pytest tests/test_index.py -v (12 passed)
BOUNDARY: Validate correctness, error handling, and test coverage; do not modify files.

Review report:
  Important: Missing progress visibility for repairs.
  Minor: Reporting interval uses an unexplained constant (100).
  Assessment: Not ready until progress visibility is added.

Parent: add the progress behavior, run tests again, then report the verified result.
```

## Red Flags

**Never:**
- Delegate an unclear task and expect the reviewer to discover its requirements.
- Treat a review report as proof that tests are unnecessary.
- Ignore Critical findings.
- Continue with unresolved Important findings without explicit user acceptance.
- Ask the reviewer to create more agents or run a parallel team.

See the review-brief template: [code-reviewer.md](code-reviewer.md)
