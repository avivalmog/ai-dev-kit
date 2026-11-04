---
name: implement-plan
description: Use when an approved implementation plan has to be carried out end to end with no mid-run questions - long multi-step implementations, work governed by rules docs or authoring guidelines that must not be violated, or when another skill delegates its implementation step. Also when the user says "implement the plan", "execute the plan", or invokes implement-plan.
---

# Implement a Plan

Run an approved plan to completion **without interrupting**, then report. Divergences are logged, not asked about. Rule compliance is checked by a cheap clean-context reviewer subagent, not by self-review. Nothing is committed without approval.

**Core principle:** you are not the audience for this work. Every decision you make instead of asking has to be legible to the user afterward, or it didn't happen.

**This is a process wrapper, not a replacement.** It owns *when* things happen and *what gets recorded*. The *how* of each step still comes from whatever skill applies — invoke them.

## When NOT to use

Mutually exclusive with `superpowers:executing-plans` (separate sessions with human checkpoints) and `superpowers:subagent-driven-development` (fan-out of independent tasks). Those are alternative *process* skills; running one inside this puts two owners on the same phases. Pick one.

## Invocation

```
/implement-plan
/implement-plan reviewer: gate
/implement-plan reviewer rules: PROMPT_AUTHORING.md
/implement-plan reviewer rules: PROMPT_AUTHORING.md, WIRE_SCHEMA.md
/implement-plan reviewer checklist: my-rules.yaml
/implement-plan <plan path or inline plan> reviewer rules: <doc>
```

Plan defaults to the one approved in this session. Resolve bare filenames by search (`rg --files` / glob); an ambiguous filename is the one thing worth a single question before starting.

| Argument | Effect on Phase 2 (always run through the `gate` skill) |
| --- | --- |
| *none* | Skipped entirely. |
| `reviewer: gate` | `/gate review`: every reviewer the repo's gates select (shared + project), logic and quality. Phase 3 = `/gate checks`. |
| `reviewer rules: <docs>` | `/gate review --only rules:<docs>`: one reviewer that **extracts** its own allowlist from the docs. |
| `reviewer checklist: <file>` | `/gate review --only checklist:<file>`: that file **is** the allowlist; exactly those items are checked. |
| rules + checklist | The checklist wins and is closed. |

The flow this skill sits in: `DEV_FLOW.md`, beside your global rules file.

Create one todo per phase, plus one per plan step at Phase 0.

## Phase 0 — Lock the plan

You cannot detect a divergence from a plan you never wrote down. Before the first edit:

1. Write the plan's steps out as a checklist, one todo each. List every file the plan names.
2. Record the baseline: `git rev-parse HEAD`, and `git status --porcelain` so you know which files were **already** dirty — those are not yours and are not reviewed.
3. Create the run directory at `<scratchpad>/implement-plan/<run-slug>/`, where `<run-slug>` comes from the workdir so parallel runs don't collide. Everything durable this run produces lives here:

   ```
   divergence-log.md      every divergence, every rule fix
   ```

   Review rounds, raw reviewer outputs and frozen allowlists live in the gate's
   state (`~/.ai-kit/gate-state/repos/<repo-id>/`, see `gate.py status`).

   Start the log:

   ```markdown
   # Divergence log — <plan name>
   base: <sha>   already-dirty: <files>

   | # | tag | plan said | did instead | why / alternative rejected |
   |---|-----|-----------|-------------|----------------------------|
   ```

   Files, not memory — a long session gets compacted before the summary is written.
4. Confirm the working branch satisfies the project's branch rules before editing. (Skip if `owns:` says the caller handles it.)

## Phase 1 — Implement and log

Work the checklist in order. Read a file before editing it. Match the surrounding code's idiom.

**Never stop to ask which option the user prefers.** When reality doesn't match the plan, take the reading the plan's evident intent supports, append one row to the log, and continue.

Tag every row:

- **`[LOGIC]`** — the plan's stated behavior, output shape, contract, or file set changed. Write down the alternative you rejected. These are what the user would have been asked about.
- **`[MECH]`** — a name, placement inside a file the plan named, a helper extracted or inlined, an identifier the plan mis-cited, additive-only work (tests, type hints, docstrings).
- **`[RULE]`** — a change made because the reviewer found a violation (Phase 2). The row names the rule id and the file, so the log alone shows reviewer-said-X -> I-changed-Y without cross-referencing two artifacts.

**Plan silence is not a divergence.** Plans never specify everything:

- Silent *inside* a step or file the plan already named → just implement it. No row. Don't clutter the log.
- Silent, but it needs a **new file** or a **new user-visible behavior** → `[LOGIC]` row.

The only hard stops that remain: a destructive, irreversible, or outward-facing action, or the plan becoming impossible with no independent work left. Neither of those is a preference question.

## Phase 2 — Reviewer loop (the gate)

Only when a `reviewer` argument was given. The `gate` skill owns the whole loop:
- selection;
- one parallel round;
- verbatim outputs;
- quote and coverage checks;
- the attempt cap;
- the stamp.

Follow its `/gate review` procedure exactly; never run a second loop of your own here.

Run it twice at minimum:

- **Tripwire**, once, right after the first change touching a risky file: `/gate review --tripwire`
  (plus the `--only` form for `rules:` / `checklist:`). A risky file is one in the gates'
  `always_when`, or, for `rules:`, the first file the docs govern. First instances become templates;
  catching the template beats rewriting nine copies of it.
- **Full pass**, after the last code change: `/gate review`, repeated until it stamps or its attempts
  run out.

**Do not give a reviewer the plan or a summary.** The prompt files `gate.py` writes are the entire
prompt. The fidelity reviewer reads the plan by design, from the run registered with `gate.py init`.
A rule violation is not excused by a plan asking for it.

What gets written down:
- Each reviewer's output goes verbatim to its `out` path, per the gate procedure.
- For each fix made from a finding, append a `[RULE]` row to the divergence log naming the reviewer, the finding id and the file.
- A rejected finding gets its `gate.py reject` counter-citation in the same row.

When the attempts run out, `gate.py` exits 3. That is the stop: report the open findings and the
user's three replies (see the gate skill's "Attempts" section). If you are running somewhere that
cannot start a fresh worker (Claude Code: the `Agent` tool), report `Phase 2 skipped — reviewer could not
be dispatched`. **Never substitute
self-review.** Self-review is the blind spot this phase exists to cover.

Handling findings, via `superpowers:receiving-code-review`: verify, don't performatively agree. A
finding the code proves wrong is rejected with `gate.py reject` and a `file:line`. A re-raised one
stays open and goes in the summary; don't argue a second time.

## Phase 3 — Verification

Run `/gate checks` (the `gate` skill). It runs the checks the repo's gates declare: the project's `reviewers/gates.toml`, or the shared default. Append any `extra_checks:` the caller passed. Only if the gate skill is unavailable: the project's own verification skill, or its README's test and lint commands. Then `superpowers:verification-before-completion` before any claim that it works.

| Breakage | Action |
| --- | --- |
| Mechanical — import, signature, fixture, path, an assertion updated to the behavior **the plan specified** | Fix it, log `[MECH]`, continue. |
| Needs a behavior call | Make the call the plan's intent supports, log `[LOGIC]`, surface it in Gaps. |

**Never make a check pass by weakening it.** No deleting, skipping, `xfail`-ing, loosening, or mocking away an assertion to get green. A test failing because the code is wrong is the test working.

## Phase 4 — Summary

Five sections, this order:

1. **Decisions I made for you** — every `[LOGIC]` row, each with the alternative rejected. First, because these are exactly what you'd otherwise have been asked.

   **Write each one in plain English, for someone who has not read the code.** One bullet, and it has to answer three things in order: *what I had to choose between*, *what I picked*, *what would have gone wrong the other way*. Lead with the plain-English statement; a file, symbol, or number goes at the end as supporting evidence, never as the thing the sentence is built out of. If a reader has to already know an internal name to parse the first sentence, rewrite the sentence.

   - Bad: “The stage table hashes the implementation that runs, not who declared it; otherwise `jv_id` is wrong and the stage diff over-reports.”
   - Good: “When I compare two judge versions, I now compare the code that actually runs at each step, rather than which version declared it. Before this, comparing two versions that differed in one step reported all six steps as different, so the report couldn't tell me what actually changed.”

   Jargon coined during the implementation — ids, internal type names, stage numbers — is explained on first use in this section or not used here at all. The rest of the summary may assume it; this section may not.
2. **What was implemented** — one bullet per plan item: done / changed / dropped. Name any phase skipped because the caller owned it.
3. **How it was tested** — first `gate.py summary --tested` printed verbatim: each test kind that took part, then one `missing` line for what did not. Then, per claim the table doesn't cover, the exact command and its **actual** output. Anything not executed is labeled `unverified`. No "should work".
4. **Rule review** — the gate's rounds. For each round: the reviewers (with any calibration warning `review-prepare` printed) and the finding counts, from `gate.py status` and each round's `ingest.json`, cited by path. Cite those files; never narrate the review from memory — you are the party being audited. Then: findings fixed, rejections with their citations, anything open at the cap, and the nits. A reviewer that ran uncalibrated is stated here before anything else.
5. **Gaps left to validate** — what the plan didn't cover, what the `[LOGIC]` decisions opened up, what no test touches.

## Phase 5 — Commit proposal

One commit message: a title, then one bullet per high-level concept implemented. Name critical code parts where a reader would need them. Then **wait for approval.** Do not commit.

## Composition

**Skills invoked per phase** — check the live available-skills list each run, never assume:

| Phase | Invoke |
| --- | --- |
| 1 | `superpowers:test-driven-development` when the plan adds behavior; any project skill matching the surface touched |
| 1, on a failure | `superpowers:systematic-debugging` before proposing a fix |
| 2 | `superpowers:receiving-code-review` for every finding |
| 3 | the project's verification skill; `superpowers:verification-before-completion` |
| 5, after approval | `superpowers:finishing-a-development-branch`, unless the caller reserved integration |

**Precedence when guidance collides:** project / user hard rules → a sub-skill's domain method (how to write the test, how to debug) → this skill's process gates (no mid-run questions, log every divergence, reviewer loop, summary shape, commit waits). They're orthogonal by construction: sub-skills say *how*, this says *when* and *what must be recorded*.

**Called-by contract.** A calling skill passes:

```
plan:         <path or inline>                          required
workdir:      <path>                                    default cwd — the caller creates it, never this skill
reviewer:     gate | rules: <docs> | checklist: <file> | none   default none
owns:         <phases the caller keeps>                 worktree | verification | commit | integration
extra_checks: <commands appended to Phase 3>            optional
```

Every phase listed in `owns:` is **skipped and named as skipped in the summary** — never silently duplicated. `extra_checks:` is the lighter alternative: the caller injects commands into Phase 3 without taking the phase over.

**Returns**, standalone or nested: the Phase 4 summary, the Phase 5 proposed commit message, and the `<run-slug>/` directory path (divergence log). The review rounds and stamps live in the gate's state (`gate.py status`). This skill never commits, never creates or destroys a worktree, never opens a PR/MR.

## Rationalizations — all of these mean STOP

| Excuse | Reality |
| --- | --- |
| "I'll remember this divergence, no need to log it" | The session gets compacted. The log is the only channel back to the user. |
| "This divergence is too small to log" | Then it's plan silence inside a named file and needs no row. If it changed a stated behavior, it's `[LOGIC]`. There is no third case. |
| "I'll ask the user, this one's important" | You don't ask. You take the reading the plan supports and tag it `[LOGIC]`. Questions were traded away for a legible summary. |
| "I already know those rules, no need to dispatch" | You read them under load, in a full context, while writing the code. That is precisely the blind spot the clean-context reviewer covers. |
| "I'll summarize the rules doc for the reviewer" | Then it audits your reading of the rules, not the rules. Give the path. |
| "The reviewer is being pedantic" | It quoted the rule or it didn't. Quoted ⇒ fix. Unquoted ⇒ it's already a flag, not a violation. |
| "I'll run the reviewer once, at the very end" | The tripwire exists because the first file becomes the template for the next eight. |
| "I'll write down what the reviewer said, in my words" | Verbatim to the round's `out` path, before you act on it. A paraphrase by the audited party is not a record. |
| "The reviewer said NONE, so it's clean" | A `NONE` proves something only from a reviewer `/gate calibrate` proved on a planted defect. From an uncalibrated one it is unproven; say so. |
| "I'm already in a skill, no need to invoke another" | This skill sequences; it does not implement. TDD, debugging, and verification still come from their own skills. |
| "It passes if I adjust this assertion" | Adjusting it to the plan's intended behavior is a fix. Adjusting it to whatever the code now does is hiding a bug. |
| "I ran the tests earlier" | Report the run from Phase 3, with its output. Earlier is not evidence. |
| "It's basically done, I'll commit it" | Propose the message. Wait. |

## Red flags

- You are about to write "I assumed…" in the summary → that was a `[LOGIC]` row you never logged.
- The divergence log is empty after a long implementation → you are not logging, you are forgetting.
- The reviewer prompt contains your description of the rules instead of a path → it inherited your blind spot.
- A verification result is described without its output pasted → you did not run it.
- You are on reviewer round 4 → stop, report the open findings, move to Phase 3.
- The summary's "How it was tested" has no command in it → it is not a test section, it is a claim.
- The summary's "Rule review" has no file paths in it → you narrated the audit instead of citing it.
- A "Decisions I made for you" bullet opens with an internal name, an id, or a stage number → you wrote it for yourself. Rewrite it as what was chosen and what the other way would have cost.
- `Violations: NONE` from a reviewer `review-prepare` flagged as uncalibrated → you do not know whether the reviewer was working.
