---
description: Run a task as a one-off — single script, no repo files, no tests, no commits
argument-hint: <what the script should do>
---

Scope: **THROWAWAY**. Everything below is a one-off with no intended lifetime past this session.

Hard rules for this task:

- Write it as a **single script** in the session scratchpad dir (or `/tmp` if there is none). Nothing anywhere inside the repo.
- No tests, no test harness, no running the repo suite.
- No branch, no worktree, no commit, no push, no MR.
- No docs, no CHANGELOG, no review-index entry, no README touch.
- Don't refactor repo code to support it — inline whatever you need, duplication is fine.
- Run the script, then report: its stdout/stderr, and for every file it produced a **SHA-256 + byte size**.
- If the job genuinely can't be done without writing inside the repo, stop and say why rather than doing it.

Skip the ceremony skills (brainstorming, TDD, plan-then-execute) for this — the scope declaration above is the plan.

Task:

$ARGUMENTS
