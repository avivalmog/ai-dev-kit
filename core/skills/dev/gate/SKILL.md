---
name: gate
description: Use to run the development gates on a change - the plan review before implementing, the logic + quality review after the logic is done, the pre-commit checks (unit, dry run, UI, real run, full suite), and the pre-push audit. Also for "explain which reviewers will run", "calibrate the reviewers", and when the parallel-feature-workflow or implement-plan skill delegates a review or check step to it.
---

# Gate

One engine for the gates in `DEV_FLOW.md`, beside your global rules file. The script decides; you
dispatch, save, fix.

```
G="uv run --no-project --python 3.12 <this skill's folder>/gate.py"
R=<absolute path of the worktree>          # pass --repo "$R" to every command
```

**Core principle:** you are the audited party. Selection, quote checks, coverage, calibration and
stamps belong to `gate.py`. You never choose which reviewer runs, never summarize the change for a
reviewer, never paraphrase a reviewer's output, and never write a stamp by hand.

| mode | invocation | stops when |
| --- | --- | --- |
| plan | `/gate plan <plan.md>` | plan PASS, or `plan_rounds` attempts used |
| review | `/gate review [--tripwire] [--only rules:<doc> \| --only checklist:<file>]` | review PASS, or `review_rounds` attempts used |
| checks | `/gate checks` | checks PASS, or `check_rounds` attempts used |
| push | `/gate push` | audit clean + push stamp, or `push_rounds` attempts used |
| explain | `/gate explain [--range A..B] [--plan P]` | free: prints tier, reviewers, checks with reasons |
| status | `/gate status` | free: where the run stands (use after a compaction) |
| calibrate | `/gate calibrate [ids]` | every named reviewer caught its planted fixture and stayed clean on the clean one |

## Register the run (once per branch)

```
$G init --repo "$R" --request <request.md> --plan <plan.md> --divergence-log <divergence-log.md>
```

`request.md` is the user's words verbatim. The plan and the divergence log feed the fidelity reviewer
and the commit-hygiene rule H1 (every committed file is in the plan's table or the log).

Every `gate.py` command ends by printing the run's dashboard path on stderr, with the Windows path too
under WSL. Whenever you tell the user a gate result (a round ingested, checks run, an audit, a stamp, a
cap reached, or the run registered), put that line in the same message. Every command rewrites that page
(rounds, findings per what the user must decide, the plan changes, the code fixes and what is left; it
reloads itself). Where the agent has a status line wired to `gate_view.py statusline` (the Claude adapter
does this), it shows the current stage, round, attempts and open findings.

**Handoff.** A passing plan stamp, and any stage running out of tries, writes `<repo>/HANDOFF.md` and
moves the plan, divergence log and request into `<repo>/.handoff/` (kept out of git); the run points at
those copies from then on, so edit them there. Every later command rewrites `HANDOFF.md` with where the run
stands and the next step. `$G handoff --repo "$R" [--note "<text>"]` writes it on demand. When the output
says `handoff:`, ask the user "build here, or in the worktree's own window?"; for the window, run
`code <repo>` and tell them to type `/resume-handoff` there, then stop working on this branch here.

## Dispatching a round (plan, review, calibrate)

`*-prepare` prints JSON with a `dispatch` list. Start **every** entry at once, so they run in parallel,
each in a fresh worker with a clean context:

- The worker is read-only, at the entry's `model_tier` (`light`, `standard` or `strong`; map it to one of your
  models). Its prompt is exactly one line:
  ``Read `<prompt file>` in full and follow it exactly; it is your complete instructions for this task.``
  Never retype the file and never add a word, so the reviewer gets only what `gate.py` wrote. Use the
  `prompt` file when you have a named agent installed for the entry's `agent_name`; otherwise use the
  `fallback_prompt` file, which carries the reviewer's instructions itself. An entry with no
  `fallback_prompt` is already complete.
- Save each reply **verbatim** to its `out` path, before acting on it, unchanged.

**Claude Code.** One message with one `Agent` call per entry: `subagent_type` = `agent_name` (or
`general-purpose` with the `fallback_prompt` when `agent_name` is null or not installed), `model` =
`haiku` for `light`, `sonnet` for `standard` and `opus` for `strong`. Save the replies with `$G capture
--dir <round or run dir> --transcripts <tasks dir>`, where the tasks dir is the folder of any Agent result's
`output_file`: it copies each final report straight from its transcript into its `out` path, matched by the
prompt file it was given. Only for a reply it can't capture: Write it to `out` by hand, unchanged.

Then `*-ingest`. It drops findings whose quote is not at the cited line, marks a reviewer invalid when it
skipped a file or echoed the wrong tree. Exit 0 = nothing blocks.

There is no per-round canary. A reviewer is proven once, by `/gate calibrate`, after its own files
change; `*-prepare` warns about an uncalibrated reviewer but still runs it.

## /gate plan

1. `$G plan-prepare --repo "$R" --plan <plan>`. A `note` (docs-only plan) ⇒ go to step 4.
2. Dispatch, save, `$G plan-ingest --repo "$R"`.
3. Open blocker/should-fix ⇒ revise the plan, or reject a wrong finding as below with
   `--stage-dir plan`. Then:
   - **Gated:** back to step 1. No human looks at the plan, so a reviewer re-checks the fix.
   - **Not gated:** do not re-review. Show the user each finding next to the plan text that fixes it,
     and stop. When the user types `#approve-plan`, run
     `$G stamp --repo "$R" --stage plan --plan <plan> --approve` and go on to implementation. The stamp
     records APPROVED, the round it follows and the findings it fixed. It is refused without the
     user's token, or when no plan round reviewed the plan.
4. `$G stamp --repo "$R" --stage plan --plan <plan>`.

A passing plan stamp prints `handoff:` (see Register the run); the plan's path is `.handoff/plan.md` from then on.

## /gate review

1. `git add <explicit paths>`. Never `-A`, `.`, `-u` or a directory; the hook blocks them.
2. `$G review-prepare --repo "$R"`. Only reviewers whose scope changed since they were last clean are
   dispatched; a `note` ⇒ go to step 6. Four rules keep rounds short:
   - **Logic first, quality last.** While any logic reviewer is dispatched, the quality reviewers wait
     (`deferred` in the output). They run as a final polish round once logic is clean, because a logic
     fix rewrites the code they would review.
   - **Exclusions.** A reviewer's `exclude` globs (`default-gates.toml`) are never sent to it: test files
     never reach `polish` or `data-schema`.
   - **Delta re-review.** A reviewer with an earlier valid review on this branch is sent only the files
     and diff changed since that review, plus its own still-open findings to re-check (`rereview-note.md`,
     appended after the calibrated prompt). Its first review, or one after an invalid report, covers the
     whole change.
   - **Re-run only where needed.** Once clean, a reviewer with `rerun_skip` (`default-gates.toml`) is not
     re-run when only those paths changed: a test-only fix re-runs the `tests` reviewer alone. A
     reviewer with open findings always re-runs.
   - **No new scope.** On a delta re-review, a new should-fix on lines that did not change since that
     reviewer's base is kept as a nit (`late` on the finding), so it shows on the dashboard but does not
     block. A blocker always blocks, and so does an earlier finding re-reported because it is not fixed.
3. Dispatch, save, `$G review-ingest --repo "$R"`.
4. Triage every open finding: verify it against the code, don't agree performatively.
   - Real ⇒ fix it. Logic-stage findings first, then quality.
   - Wrong ⇒ `$G reject --repo "$R" --finding <reviewer>#<n> --cite <file:line> --reason "<why>"`.
     Add `--stage-dir plan`, `tripwire` or `adhoc` for those rounds. One rejection per finding;
     a re-raised one stays open and goes in the MR.
   - Either way, tell the dashboard: `$G note --repo "$R" --finding <reviewer>#<n> --done "<Fixed: what
     changed, in easy English>"` (or "Rejected: why"). Add `--plain "<what goes wrong, easy English>"`
     when the reviewer's `plain` line is missing or unclear. Dashboard-only; it changes no decision.
5. Re-stage the fixed paths, back to step 2.
6. `$G stamp --repo "$R" --stage review`.

`--tripwire` (from implement-plan, right after the first edit to a risky file): only the project
reviewers covering risky files run, once, no attempt counted, no stamp. Fix what they find on the spot.
`--only rules:<doc>` and `--only checklist:<file>` are the `/rules-review` alias: one reviewer,
report-only unless asked to fix. Pass the same `--only` to `review-ingest`. Its rounds live in their own
`adhoc` dir: no attempt counted against the gated review, and no stamp. When a configured reviewer owns
that doc, that reviewer runs, with its calibration status. For any other doc there is no calibration,
and the report must say the review is unproven.

## /gate checks

1. `$G checks-run --repo "$R"`. It runs every selected check that is not passing for this tree, in
   order (mechanical, unit, dry-run, ui, real, then `last`), and stops later groups after a failure.
   Start it in the background (Claude Code: `run_in_background`) while the review round runs; both re-run
   only what a fix touched.
2. A red check ⇒ read its log and fix **the code**. Never skip, xfail, loosen, delete or mock away a
   check or an assertion.
   A red check always blocks, even when base is red too. To learn whose failure it is,
   `$G checks-run --repo "$R" --baseline <id>` re-runs it on a clean base worktree and records the
   verdict; the check stays failing either way. Already broken on base ⇒ fix it, or at the cap ask the user.
3. `$G stamp --repo "$R" --stage checks`.

## /gate push

1. `$G audit --repo "$R" --mode branch --fetch` checks:
   - H1–H4 over the branch;
   - forbidden paths and secrets;
   - whether base moved under the branch. If it did, it re-runs the affected checks on the merge. A
     conflict uses up the push attempts at once, because only a rebase (outside the authorization) can fix it.
2. `$G stamp --repo "$R" --stage push`, then push. The guard hook re-checks every stamp for `HEAD^{tree}`.
3. The MR description = implement-plan's summary + `$G summary --repo "$R" --head`.

## Attempts, and the only stop

An attempt is a **failed** run: a review or plan round with blocking findings or an invalid reviewer;
a checks run with a red check; a branch audit with hits. A run that passes costs nothing.
Once a stage's failures reach its cap, `gate.py` exits 3 before running again. Then **stop and ask
the user**. Show:
- what each attempt tried;
- the open findings or the failing log tail;
- the three replies they can give:
  - `#accept-gate`: continue, findings go in the MR. Then `$G stamp ... --accept`.
  - direction: then `$G reset-attempts --repo "$R" --stage <stage>`.
  - `#abort`: stop here.

## Every session ends with

`$G summary --repo "$R" --tested` (`--head` after committing) printed verbatim in the final message:
one row per test kind, always, with SKIPPED and unverified rows visible.

## /gate calibrate

Required after editing a reviewer file (`core/reviewers/<id>.md` in the kit) or its `model`, the dispatch
template, the rules template or the output contract. `review-prepare` and `plan-prepare` only warn about a
reviewer that was never calibrated or changed since; it still runs.

1. `$G calibrate-prepare --repo "$R" [ids]`.
2. Dispatch every entry at once and save each reply to its `out` (Claude Code: one message, then
   `$G capture --dir <run_dir> --transcripts <tasks dir>`, which also records each duration).
3. `$G calibrate-score --run-dir <run_dir>`.
4. A FAIL: fix the agent's instructions, or a fixture that doesn't test what it claims, then re-run that id.

## Rationalizations that mean STOP

| Excuse | Reality |
| --- | --- |
| "This reviewer is irrelevant, skip it" | Selection is the script's. Change `gates.toml` in its own reviewed change, never mid-gate. |
| "I'll summarize the diff in the prompt" | The prompt file is the whole prompt. Adding text inherits your blind spot. |
| "I'll paste the gist of the reply" | Verbatim to `out`, or the record is yours, not the reviewer's. |
| "The check is flaky, mark it skipped" | Fix it, or stop at the cap and ask. `--baseline` only says whose failure it is. |
| "It also fails on main, so it's not mine" | It still blocks the commit. Fix it, or stop at the cap and ask. |
| "Attempts are used up but I'm close" | Exit 3 is the stop. Ask the user. |
