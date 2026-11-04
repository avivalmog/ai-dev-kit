# General rules for coding agents

These rules apply to every project, whichever coding agent reads them. Personal preferences live in
`CLAUDE.md` beside this file, which loads this one. A project's own rules file (`AGENTS.md` or `CLAUDE.md`)
may **tighten** these rules but never loosen them: the stricter rule wins. Detail lives in `docs/` beside
this file; each pointer below says when to read it.

# Git Rules

## HARD RULE — never commit without explicit command

NEVER run `git commit`, `git push`, `git tag`, `git rebase`, `gh pr ...`,
`glab mr ...`, or any command that creates or moves a persistent git ref,
unless the user has explicitly told you to in the current message or in a
clearly-scoped prior instruction in the same session.

**Why:** a commit is one logical change, shaped by the change, not by your turn
boundaries; how work is split into commits is the user's call.

## HARD RULE — never discard uncommitted work

`git checkout -- <path>`, `git restore`, `git clean`, `git stash drop`, and
`git reset --hard` destroy work that exists nowhere else. Each needs explicit
permission, exactly as a commit does.

## HARD RULE — all code changes on a branch, never on main

Before editing any code, check `git branch --show-current`. If it's `main` (or
`master`), create the working branch before the first edit: `git checkout -b
<descriptive-branch>`, or a worktree where the project's rules file requires
one (then never `checkout -b` inside the main worktree). Branch/worktree
creation needs no approval; commits still do.

## Commit messages — compact, general topics

A single-line subject naming the area touched; no body, no per-bullet changelog.

## HARD RULE — never install into the global Python

Never `pip install`, `pip uninstall`, `pip install -e .`, or `pipx install`
into the global / system Python. Target, in order: the venv the project's
rules file names for this worktree; else a `.venv/` at the repo or worktree
root (use it without asking); else, or with several candidates, ask.

# Gated delivery — the flow is `DEV_FLOW.md`, beside this file

## HARD RULE — commits and pushes go through the gates (hook-enforced in Claude Code)

Every commit needs passing `review` and `checks` stamps for its exact staged
tree, plus clean commit hygiene; every push or MR also needs the `push` stamp.
Run `/gate review`, `/gate checks` and `/gate push`. Never arm, suggest, or work
around `#skip-review`, `#skip-gates` or `#accept-gate`: only the user types
them. Name every staged file explicitly.

## A `gated` request is a clearly-scoped instruction

When the user's request says `gated`, then for that request only: the
plan-gate PASS replaces the plan-mode approval stop; the commits (one concept
each, compact subjects), the push and the MR open are authorized once their
gates pass; real-run checks are allowed up to the project's `max_usd`. It never
authorizes merging, rebasing, force-pushing, tagging, or anything destructive
outside the branch.

## Two rules for every development session, gated or not

1. The final message carries **How it was tested** from `gate.py summary --tested`,
   verbatim: what took part, then what is missing.
2. The flow stops and asks only when a step has used all its attempts, when the
   agent set a serious finding aside, when a reviewer found an older bug, or when
   a hard rule in this file stops it.

# Testing Rules

## Targeted tests by default; full suite ONLY at the commit gate

Run the specific test file(s) touching the change. The full suite runs only in
the turn of an authorized commit, or after a structural CODE change. Editing
data, config, fixtures, prompts or docs is never structural. Never re-run a
passing full suite, never twice in one turn. **Before running more than one
test file, read `docs/testing.md`.**

# Verification Rules

## Cite it, run it, or say "unverified"

- Cite `file:line` for every behavioral claim about code, CLI flags, defaults,
  or gating logic — and never claim "X does not exist" off a single
  non-recursive glob; sweep with `rg` / `grep -r` first.
- For a claim about *runtime* behavior, a citation is not proof — write a small
  script under the scratchpad dir, run it, and paste the actual output.
- If something cannot be verified in the moment, say "unverified" — never
  assert it in a confident sentence instead.

# Planning Rules

- **In plan mode, read `docs/planning.md` first:** every plan opens
  with a producer/consumer file table, then stops for the user's approval.
- Outside plan mode, do that sweep silently and offer the table in one line.
- If the user attaches a matrix, table, or spec, reproduce its exact orientation
  and values before proposing changes.
- Prefer the smallest solution: one-off / throwaway scripts stay throwaway (no
  repo tests, no worktree) unless the user says to productionize.
- Ask at most ONE round of clarifying questions, then propose a plan; do not
  chain question rounds.
