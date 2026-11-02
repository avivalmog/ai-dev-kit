---
name: rules-review
description: Use when changed, staged, or uncommitted files should be checked against a rules or authoring doc - "review my changes against PROMPT_AUTHORING.md", "did I break the wire schema rules", a compliance pass over a diff before committing work governed by a guidelines document. Not for bug hunting or code-quality review.
---

# Rules Review

A one-off compliance pass of the changed files against a rules or authoring doc. It is an **alias of
the gate engine**. The engine, the one reviewer template (`rules-reviewer-prompt.md`), the calibration
fixtures and the verbatim records live in the `gate` skill's folder, beside this one. The flow is `DEV_FLOW.md`,
beside your global rules file.

**Core principle:** you are the party being audited. You don't choose which rules get checked, you don't
summarize the rules for the reviewer, and you don't decide the review worked. Calibration decides that.
A doc that a configured reviewer owns (in `reviewers/gates.toml`) is reviewed by that reviewer, which
`/gate calibrate` has proven on a planted fixture. Any other doc has no calibration, so its review is
unproven and the report says so.

## When NOT to use

- **Correctness, design or quality review** → `/gate review` (every reviewer) or `/code-review`.
- **Inside `implement-plan` or a gated run** → their Phase 2 already runs `/gate review`. Don't stack a
  second owner on one phase.

## Invocation → gate

| You type | Runs |
| --- | --- |
| `/rules-review rules: docs/PROMPT_AUTHORING.md` | `/gate review --only rules:docs/PROMPT_AUTHORING.md`, report-only |
| `/rules-review rules: A.md, B.md` | `--only rules:A.md,B.md` |
| `/rules-review checklist: my-allowlist.yaml` | `--only checklist:my-allowlist.yaml` |
| `... files: <paths>` | those paths are the target (staged first, below) |
| `... fix` | fix the blockers, re-stage, next round: at most `[caps] review_rounds` rounds, none of them counted against the gated review |

Resolve a bare doc name by search (`rg --files`). An ambiguous filename is the one thing worth a single
question before starting.

## Steps

1. **Target.** The gate reviews the staged change. Pick the target from the first that applies:
   1. `files:`;
   2. what is already staged;
   3. the unstaged and untracked files of this session.

   `git add` it with every path named explicitly. Announce the source and the file count. Empty ⇒ stop
   and say so; never widen to the whole branch.
2. **Round.** Follow the gate skill's `/gate review` procedure with `--only …`:
   1. `review-prepare`;
   2. dispatch the prompt file verbatim;
   3. save every reply verbatim to its `out` path;
   4. `review-ingest` with the same `--only …`.

   Do not pre-digest the rules doc and do not pre-filter files. The reviewer extracts the rules; its
   `applies_to` globs skip out-of-scope files.
3. **Default is report-only:** no file is edited. With `fix`, fix the blockers, re-stage, and run a fresh round.
4. **Report**, in this order:
   1. **Calibration**, stated first: the reviewer's status as `review-prepare` printed it. "Never
      calibrated" or "edited since" means the reviewer is unproven; a doc no configured reviewer owns is
      unproven too, so a clean result proves nothing.
   2. **Violations**, each with its verbatim rule quote and `file:line`.
   3. **Flags** (the nits).
   4. **Not checkable:** the reviewer's list of rules it could not check. These are this run's blind spots.
   5. **The round directory**, by path.

   Cite the saved outputs; never narrate the review from memory.
