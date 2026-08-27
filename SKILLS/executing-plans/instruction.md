# Executing Plans

## Overview

Execute a Plan through JKagent's single session runtime. Plan creation auto-approves and starts execution, so all material ambiguities must be resolved before creation. For work spanning multiple milestones, interruptions, or pause/resume requirements, use an active/armed Goal with autonomous same-session rounds.

**Announce at start:** "I'm using the executing-plans skill to implement the approved plan."

**Runtime contract:** Do not assume Claude Code, Codex, external CLIs, worktrees, or fresh agents. The current session owns execution and final verification. Use `create_subagent` only for a bounded independent subtask; it creates one direct child and that child must not create further agents.

## The Process

### Step 1: Confirm the execution boundary

1. Read the Plan and its source requirements.
2. Confirm the auto-started execution state, scope, acceptance criteria, and verification commands.
3. Inspect the current workspace and identify conflicts, missing dependencies, or changed assumptions.
4. If an ambiguity affects behavior, security, scope, or architecture, ask the user before starting; do not guess.
5. Create a task checklist for the Plan items. If the work needs durable long-running tracking, create or continue the linked Goal instead of inventing a parallel workflow.

### Step 2: Execute tasks sequentially

For each task:
1. Mark it in progress.
2. Follow the specified steps and keep changes within the approved scope.
3. Run the task's verification commands.
4. Record the result, changed files, and any deviation from the Plan.
5. Mark it complete only after its acceptance checks pass.

### Step 3: Delegate only bounded independent work

A Subagent is appropriate only when all of the following are true:
- the task has a clear input, output, and acceptance criteria;
- it can progress without conversation context beyond a concise brief;
- its result can be reviewed as a structured report;
- it does not need to create another Subagent, Plan, or Goal.

Give the Subagent a prompt containing the exact task, constraints, files or commands to inspect, expected deliverable, and whether it may modify files. Continue parent work where possible, then review the returned report and verify any claimed result yourself.

Do not create one Subagent per Plan task, and do not use a Subagent to avoid asking the user about an ambiguity.

### Step 4: Complete and report

After all tasks are complete:
1. Run the full required regression suite and any targeted checks.
2. Inspect the final diff for unintended changes.
3. Compare outcomes against every Plan acceptance criterion.
4. Report completed work, verification evidence, residual risks, and any deferred items.
5. Only mark a Goal complete when its linked Plan and quality/acceptance checks have actually passed.

## When to Stop and Ask for Help

**Stop execution immediately when:**
- a required dependency or credential is unavailable;
- a test fails repeatedly and the cause is not understood;
- the Plan has a material gap or conflicts with the current codebase;
- an instruction is ambiguous in a way that changes behavior;
- continued work would exceed the approved scope or create a security risk.

State the concrete blocker, evidence collected, and the smallest decision needed from the user. Do not force through blockers or substitute an unapproved design.

## Remember

- Plan creation starts execution automatically; ask before creation when scope is ambiguous.
- Preserve one runtime and one task lifecycle.
- Verify each deliverable before continuing.
- Use a Goal for durable long-running work, not an improvised second workflow.
- Use only one Subagent layer, only for bounded work.
- Never claim success without the specified verification evidence.
