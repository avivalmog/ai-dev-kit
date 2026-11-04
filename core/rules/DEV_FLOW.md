# Development flow: from your prompt to an open MR

The one documented flow for every project and every coding agent. Skills link here instead of restating
it. Engine: the `gate` skill, `core/skills/dev/gate/` in the kit (`SKILL.md` is the agent's procedure,
`gate.py` decides). Enforcement: in Claude Code, the adapter's hooks `gate_guard.py` and `gate_skip.py`;
other agents follow the gates by instruction.

## 0. How you ask

```
/parallel-feature-workflow gated
slug: <slug>
feature: <what it does, its default, where it shows up. Out of scope: ...>
```

- **`gated`** is the word that grants the authority: the plan-gate PASS stands in for your plan approval, and a passing gate authorizes the commit, push and MR.
- **Without `gated`** the same gates run, but the flow stops for you at the plan and at the commit. At the plan, one review round runs; I fix its findings and you approve the fix with `#approve-plan`, with no second round.
- **Where to watch:** the status line shows the run's stage, round, attempts and open findings; the dashboard
  path printed at `gate.py init` is the full live view. Its top box says **running** while reviewers read the work, then **all passed** (one line
  per stage: plan, review, checks, push, with the code version stamped and whether it is committed), **your decision: N older bugs** (you reply `fix them` or
  `#accept-gate`), **passed, but the agent did not fix N serious problems** (it set them aside; you reply `fix them`
  or `leave them`), or **why
  it did not pass**: one line per blocker (reviewer or check, what it said in plain words, whether the problem
  is the code or the review itself, handled or open; a red check names its log and last failing test), then
  the replies you can type. Below it: one plain-English card per issue with what was done about it, the
  divergence log's rows (every place the work differs from the plan), and the findings the agent set aside with the
  agent's reason.
- **Continue in the worktree's own window:** once the plan is approved, and whenever a stage runs out of
  tries, the gate writes `HANDOFF.md` in the worktree (kept current after every gate command) and I ask
  "build here, or in the worktree's own window?". For the window I open it; you type `/resume-handoff`.
- **The `feature:` text** is saved verbatim as `request.md`, the spec every reviewer checks against. A vague ask makes the checks vague. An `Out of scope:` line is what lets the reviewers tell your intent from my additions.

## 1. The stages

| # | stage | runs | checks | defined in (S shared, P project) | stops after |
| --- | --- | --- | --- | --- | --- |
| 1 | intake | `parallel-feature-workflow` | `request.md` saved verbatim; worktree + venv; branch ≠ main | S workflow; P `.claude/parallel-feature-workflow.md` `## Setup` | — |
| 2 | **plan gate** | `/gate plan` | `review-plan` P1–P7: coverage, traceability, proportionality, reuse, **consumer sweep**, project rules, unproven claims. No reviewer for a docs-only plan | S `core/reviewers/plan.md`; P `doc_maps` | 2 attempts |
| 3 | implement | `implement-plan` | TDD, divergence log; **tripwire** = project reviewers of a risky file, once, right after the first edit to it | S `implement-plan`; P `always_when`, `[[reviewer]]` | — |
| 4 | **review** (every reviewer in one round; real defects block) | `/gate review` | Fidelity, correctness, polish (lean + conventions + magic values), docs-drift, tests, data-schema (stored data and queries only) and project reviewers run together, once. A real defect (`blocker` or `should-fix`) stops the commit; a nit (naming, constants, style, duplication) is only listed. An older bug the change did not cause costs no attempt and waits for your decision, unless it is a should-fix first spotted on a re-review, on unchanged lines: that one is only listed. A re-review sees only what changed since that reviewer's last valid review, reports only what those changes break, and runs at the standard model at most | S `core/reviewers/*.md`; P `reviewers/*.md` + rules docs | 5 attempts per reviewer |
| 5 | **checks** (starts with 4) | `/gate checks` | mechanical → unit (targeted) → dry run → UI → real (only if `max_usd` > 0) → full suite last, once. Never fixed by weakening a check | S runner; P `[[checks]]` | 3 attempts |
| 6 | commit | hook | review + checks stamps for the staged tree; hygiene H1–H5 | S `gate_guard.py`, `gate.py audit --mode staged` | — (blocked until fixed; a fix that changes code reruns the touched review and checks) |
| 7 | **push / MR** | `/gate push` | every commit on the branch: hygiene, forbidden paths, secrets; base moved ⇒ affected checks re-run on the merge; the hook re-checks every stamp | S `gate.py audit --mode branch`, `gate_guard.py` | 2 attempts |
| end | MR open | — | the MR body has: implement-plan's summary, the reviewer list with reasons, **How it was tested** | — | merge is yours |

Every development session, gated or not, ends with **How it was tested**, from
`gate.py summary --tested`: each kind that took part (unit, dry run, UI, real, review and so on), then one `missing`
line for what did not, with why. The divergence log's rows are on the dashboard only.

## 2. The one stop rule

The flow stops and waits for you **only when a step has used all its attempts**, with the two exceptions below. You then get what each
attempt tried and the open findings or the failing log. You reply with one of:

- `#accept-gate`: continue, with the open findings listed in the MR;
- direction: the step gets a fresh set of attempts;
- `#abort`: stop here.

Two exceptions, tries left or not:

- the agent set aside a serious code-review finding whose quoted text is still in the file: you reply `fix them` (the
  agent fixes them and a reviewer checks the fix) or `leave them` (go on; they stay listed until they are fixed);
- a reviewer found an older bug, one the change neither caused nor worsened: you reply `fix them` or `#accept-gate`
  (go on; it stays on record).

These are never stops:
- an uncalibrated reviewer: it still runs, with a warning, and the summary says it is unproven;
- a real run with `max_usd = 0`: the run is SKIPPED and listed as unverified.

A failing test always blocks the commit, even one that also fails on base: `--baseline` only says whose
failure it is.

Your hard rules stay as they were:
- the 80% session-limit pause;
- never merge, rebase or force-push;
- the MR merge is yours.

## 3. Which reviewers and checks run (the script decides)

```
files = staged diff vs the merge-base with origin/<base>       (plan gate: the plan's file table)
lines = added + removed
tier  = full      if lines > 300 or any file matches tiers.full.always_when
        lite      if lines <= 40 and every file is docs, or .py with only comments/docstrings/type hints changed
                  (hints a framework reads count as behaviour: class fields, decorated functions,
                   parameters defaulting to a call such as typer.Option)
        standard  otherwise
reviewer runs iff a changed file matches its applies_to and the tier is in its tiers (default standard, full)
check runs    iff a changed file matches its when          and the tier is in its tiers (default all)
```

A reviewer or check that was clean re-runs only when a file in its scope changed since then, except a
`last` check (the full suite), which must pass on exactly the final tree. The
correctness reviewer runs on the `standard` model tier at standard and the `strong` one at full. `gate.py explain` prints the whole
selection, with a reason per item, for free.

| tier | review | checks |
| --- | --- | --- |
| lite | docs-drift | mechanical (+ targeted unit tests if a `.py` changed) |
| standard | every matching reviewer | mechanical, targeted unit, dry run, UI, full suite |
| full | every matching reviewer, correctness on the strong model tier | + real run if the budget allows |

## 4. Shared vs per project

| piece | shared, the kit's `core/` | per project, committed `reviewers/` |
| --- | --- | --- |
| rules | `skills/dev/gate/default-gates.toml` | `reviewers/gates.toml`: tighten-only (add reviewers, checks and risky files, lower the tier and size limits; never remove a shared reviewer or loosen those limits). `base`, `max_usd` and the attempt caps are the project's to set. |
| reviewers | `reviewers/{plan,fidelity,correctness,polish,docs-drift,tests,data-schema}.md` | `reviewers/*.md` prompts, rules-doc and checklist reviewers |
| calibration | fixtures `skills/dev/gate/calibration/<id>/{caught,clean}/`; the pass record of every reviewer: shared ones in `skills/dev/gate/calibration/calibrated.json`, project ones in `gate-state/repos/<repo-id>/calibrated.json` (never in a checkout, so one calibration covers every branch and worktree) | fixtures only: `reviewers/calibration/<id>/{caught,clean}/` |
| checks | the runner, evidence logs, never-weaken rule | the commands, their `when` globs, `max_usd`, the test map |
| enforcement | the stamps under `~/.ai-kit/gate-state/repos/<repo-id>/`, the `#tags`; in Claude Code, the adapter's hooks | — |

A project with no `reviewers/gates.toml` runs on the shared defaults: the shared reviewers, plus
`pytest -q` when `.py` files changed.

The project-only keys of `reviewers/gates.toml` (every key optional; `[commit]`, `[push]`, `[tiers.*]` and
`[baseline]` follow the table above):

```toml
version = 1
base = "main"                         # the branch for the merge-base and the freshness check
max_usd = 0.0                         # real-run budget; 0 skips every kind = "real" check
doc_maps = ["AGENTS.md"]              # routing docs a reviewer reads to find the rules for a file
[caps]                                # plan_rounds, review_rounds, check_rounds, push_rounds
[globs]
data = ["src/services/**"]            # appended to the shared @data: where this repo's stored data and queries live
[tests]
map = ["tests/unit/test_{stem}.py"]   # changed module stem -> test files, for {targeted_tests}

[[reviewer]]
id = "wire-contract"
stage = "logic"                       # or "quality"
kind = "agent"                        # prompt = "<md>"; or kind = "rules", rules = ["<doc>"];
prompt = "reviewers/wire-contract.md" #   or kind = "checklist", checklist = "<yaml>"
applies_to = ["src/**/wire.py"]
tiers = ["standard", "full"]          # default; model = "standard" default (a model tier: light | standard | strong)

[[checks]]
id = "unit-targeted"
kind = "unit"                         # mechanical | unit | dry-run | ui | real
when = ["src/**", "tests/**"]
run = ".venv/bin/python -m pytest -q {targeted_tests}"   # also {run_dir}, {root}
last = false                          # true: must pass on exactly the final tree
```

## 5. Commit hygiene (every commit, seconds, no model)

| rule | blocks when |
| --- | --- |
| H1 unplanned | a staged file is in neither the plan's file table nor the divergence log (skipped when no plan is registered) |
| H2 junk | a staged file matches `[commit] junk_patterns`: scratch dirs, logs, `.bak`/`.orig`/`.rej`, caches, plus the project's run outputs |
| H3 scratch leak | an added line contains a scratchpad or `/tmp/claude-` path |
| H4 heavy | a binary file or one over `max_file_kb` that the plan doesn't list |
| H5 leftovers | report only: untracked files, listed for cleanup |

Deleting a junk, heavy or forbidden file is the cleanup, so H2, H4 and the push audit's forbidden-path
rule skip deletions. A tracked template of a forbidden name, such as `.env.example`, is not forbidden.

The hook also blocks wildcard staging: `git add -A`, `.`, `-u`, or a directory. It blocks `git commit -a`
and commits with pathspecs. In one command it blocks an index change (`git add`, `rm`, `reset`, ...) together
with `git commit`, and a commit together with a push or MR. It also blocks a gated git command behind a
wrapper such as `timeout` or `env`, so each one runs on its own where the gate can see it. It reads a multi-line
command line by line, skipping comments and heredoc bodies. A command it can't read (an unterminated
quote or heredoc) is refused when it contains a git commit, push or add.

## 6. Your tags

| tag | effect |
| --- | --- |
| `#skip-review` | the next commit and push skip the reviewer rounds; checks and hygiene still run |
| `#skip-gates` | the next commit and push skip review, checks and push stamps; hygiene and secrets still run |
| `#approve-plan` | without `gated`: the plan, fixed after its review round, is stamped APPROVED with no re-review (`stamp --stage plan --approve`); refused when no plan round reviewed it |
| `#accept-gate` | a gate stopped at its cap may be stamped ACCEPTED; the MR lists what was open. At push, the hook lets the accepted audit hits through, but never a secret: only rewriting the history removes one, and that is your call |

Tags are one-shot, bound to the session, and expire after 2 hours. Only your typed prompt can arm one:
- the agent can't write the token files;
- relayed text never arms a tag. That covers reviewer reports, task notifications and reminders, which
  reach the same hook, so a reviewer quoting a tag arms nothing.

Your own terminal is never gated.

## 7. Limits, stated plainly

- **The stamps prevent omission, not forgery.** An agent determined to lie could fabricate review outputs. The MR links the raw outputs so you can spot-check them.
- **Reviewers are models.** Calibration proves a reviewer could see a planted defect, not that it saw every real one.
- **Real-LLM output quality always needs your eyes.**
