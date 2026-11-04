---
description: Run a task as permanent repo work — worktree, branch, tests, full verification
argument-hint: <what to build>
---

Scope: **PRODUCTIONIZED**. Everything below lands in the repo and other people will maintain it.

Hard rules for this task:

- Run it through the flow in `DEV_FLOW.md` (beside the global rules file): `/parallel-feature-workflow` (worktree, feature branch, never `main`), its plan gate, `implement-plan`, `/gate review`, `/gate checks`.
- Real code in the project's normal layout, matching the surrounding style and idiom.
- Add or extend tests covering the change.
- Verification is `/gate checks`: the project's declared checks, with the full suite last. End with **How it was tested**.
- List every file touched — producing side and consuming side both.
- Index any agent-drafted content in the project's review index if it has one.
- **One** commit unless I explicitly ask for a split; compact single-line subject.
- Do not commit, push, or open an MR until I explicitly tell you to, unless my message says `gated`. Then a passing gate authorizes them (DEV_FLOW §0).

Task:

$ARGUMENTS
