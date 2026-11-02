---
name: parallel-feature-workflow
description: Use when the user asks to build one or more features in the current repo using git worktrees — phrases like "use the parallel-feature workflow", "build features in parallel", "spin up a worktree for X", or a feature list with slugs + descriptions and a mode (sequential or parallel-agents). Also use when the user says "clean <slug>" after an MR has been merged.
---

# Parallel feature workflow

Works in any git repo. Commands are POSIX shell (Linux, WSL, macOS).
Facts that belong to one project — branches, setup, verify commands, traps —
live in that project's **project file**, not here.

This skill is the conductor of `DEV_FLOW.md`, beside your global rules file.
The gates (plan, review, checks, push) come from the `gate` skill. The
implementation comes from `implement-plan`.

## Modes

- `sequential` (default) or `parallel-agents`: how slugs are worked, see Phase 2.
- `gated`: combines with either. The request's `gated` is the user's authorization:
  - the plan-gate PASS replaces their plan approval;
  - a passing gate authorizes the commit, push and MR.

  The flow stops only when a step uses all its attempts (DEV_FLOW §2). Without
  `gated`, the same gates run, and the flow stops for the user at the plan and at
  the commit.

`G="uv run --no-project --python 3.12 <gate skill folder>/gate.py"` in the steps
below; the `gate` skill's folder sits beside this skill's folder.

## Project file

Read `.claude/parallel-feature-workflow.md` in the main checkout before Phase 0.
Each section it has replaces the default below:

| Section | Default when the section or the file is absent |
| ------- | ---------------------------------------------- |
| `## Branches` | base `main`, MR target `main`, cleanup check `main` |
| `## Pre-flight` | nothing extra |
| `## Copy into worktree` | `.env`, if it exists |
| `## Link from main` | nothing |
| `## Setup` | Python project: `python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"` (plain `-e .` when there is no `dev` extra). Otherwise nothing. |
| `## Verify` | `.venv/bin/python -m pytest -q`. **Ignored when the repo has `reviewers/gates.toml`:** its `[[checks]]` run through `/gate checks` instead. |
| `## Gotchas` | none |

Base or target branches the user gives in the request beat the project file.
No project file: say so once, use the defaults, and offer to write one when the
first feature is done.

## Shell rules

- Shell variables and `source .venv/bin/activate` do not survive between tool
  calls. Chain dependent steps with `&&` in one call, or use absolute paths.
- Call the venv by path — `.venv/bin/python -m pytest`, `.venv/bin/ruff` — never
  a bare `python` or `pytest`. The main checkout's `.venv/bin` may be first on
  `PATH`, and its editable install imports main's `src/`: a bare `pytest` in a
  worktree tests code you did not change, and your edits appear to break nothing.
- The main checkout's root, from the main checkout or from any worktree:

  ```bash
  MAIN=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
  ```

  `--git-common-dir` is the `.git` that every worktree shares, so its parent is
  main's root. Needs git 2.31 or later.

## Phase 0 — Pre-flight

```bash
git status
git branch --show-current
git check-ignore -v .worktrees/x/y .venv .env
```

- The tree must be clean.
- `check-ignore` prints only the ignored paths. Every path the project uses must
  print a line; a missing line is a path that is NOT ignored.
- Check a path *inside* the folder (`.worktrees/x/y`), not the bare name. A
  trailing-slash pattern only matches directories, so a bare `.worktrees` that
  does not exist yet reports NOT IGNORED although the rule is fine.
- Run the project file's `## Pre-flight` steps.
- Wait for **"OK START DO IT"**. A `gated` request is itself the go-ahead.

**If `.worktrees/` is not ignored:** do NOT commit a `.gitignore` fix to the
default branch — that violates the global "never commit on main" rule and dirties
the tree this phase just demanded be clean. Append `.worktrees/` to
`.git/info/exclude` instead (local, no commit, tree stays clean), and put the
permanent `.gitignore` line on the feature branch so it ships with the MR.

**One-time per clone — make `git push` safe in every worktree:**

```bash
git config push.default current
git config push.autoSetupRemote true
```

Worktrees share `.git/config`, so setting this once covers every slug. `current`
pushes to the same-named branch on the remote instead of to the upstream;
`autoSetupRemote` sets the upstream on that first push. Without both, a branch
created off `origin/<base>` inherits `origin/<base>` as its upstream and a bare
`git push` dies with *"fatal: The upstream branch of your current branch does not
match the name of your current branch."* Re-running these two lines is harmless.

## Phase 1 — Worktree + venv (per slug)

**Venv: ALWAYS a fresh worktree-local `.venv`.** This skill creates one and
installs into it. That IS the standing answer to the user's global "ask which
venv before pip install" hard rule while this skill is active — do NOT re-ask.
The main checkout's `.venv` is intentionally NOT used; per-worktree isolation is
the point.

Base off `origin/<base>` after a fetch, not the local branch — a local branch is
often several commits behind and silently bases the feature on stale code.

```bash
git fetch origin
git worktree add --no-track .worktrees/<slug> -b feat/<slug> origin/<base>
git config --get branch.feat/<slug>.merge
```

The last line must print **nothing**. `--no-track` is not optional: a branch
created from a remote-tracking ref takes that ref as its upstream, so
`feat/<slug>` would track `origin/<base>` and every later push breaks. If it
prints `refs/heads/<base>`, run `git branch --unset-upstream` before anything
else. Phase 3's `git push -u` sets the correct upstream (`origin/feat/<slug>`).

### Copy local files

For each path in `## Copy into worktree`:

```bash
cp -r <path> .worktrees/<slug>/<path>
```

A copy is a snapshot: if main's copy changes mid-feature, recopy.

### Link dependency folders, never copy them

For each folder in `## Link from main` (for example `frontend/node_modules`),
run inside the worktree, in one call:

```bash
cd .worktrees/<slug> && MAIN=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)") && ln -s "$MAIN/<dir>" <dir>
```

- Do not install per worktree and do not copy the folder — the link is instant
  and costs no disk.
- Never install *through* the link (`npm install` in a linked `node_modules`
  mutates main's tree). If the branch changes the dependency manifest or
  lockfile, replace the link with a real install.
- Caches inside the target (for example Vite's `.vite`) are shared, so main and
  the worktree rebuild them when you alternate between them.
- **Remove a link with `rm <dir>`** — no `-r`, no trailing slash.
  `rm -rf <dir>/` follows the link and empties main's folder.
  `git worktree remove` is safe: it deletes the link, not the target.

### Setup

Run `## Setup` from the worktree root, chained in one call
(`cd .worktrees/<slug> && ...`). Its last line must print a path under
`.worktrees/<slug>/`.

### VS Code (Python projects)

Only when git ignores `.vscode/` (`git check-ignore -q .vscode/settings.json`):

```bash
cd .worktrees/<slug> && mkdir -p .vscode && cat > .vscode/settings.json <<'EOF'
{
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
  "python.terminal.activateEnvironment": true,
  "python.terminal.activateEnvInCurrentTerminal": true
}
EOF
```

This makes VS Code activate **this** worktree's `.venv` in every integrated
terminal it opens for the folder. Without it a terminal starts unactivated and
`pytest` resolves to whatever is on `PATH`. The quoted `'EOF'` keeps
`${workspaceFolder}` literal. `defaultInterpreterPath` applies only when VS Code
has no interpreter stored for the folder yet — if it has one, run **Python:
Select Interpreter** once and pick `.venv`.

Branch prefixes: `feat/`, `fix/`, `docs/`.

### Handoff + open the window

Every run gets a handoff, so it can always be continued in the worktree's own VS Code window. `gate.py`
writes it; never write `HANDOFF.md` by hand.

- **When:** automatically when the plan stamp passes (`#approve-plan`, or a PASS), and whenever a stage
  runs out of tries. From then on every gate command rewrites it, so it always says where the run
  stands and what is next. `$G handoff --repo <wt> [--note "<what the gate state does not say>"]`
  writes it on demand.
- **What it does:** copies the request, the plan and the divergence log into `<wt>/.handoff/` and points
  the run at those copies. From then on edit `.handoff/plan.md` and `.handoff/divergence-log.md`, not the
  scratchpad. It writes `<wt>/HANDOFF.md` (where, files, state, next step, rules). Both stay out of git
  (the repo's `info/exclude`).
- **Then ask the user, in one line:** "Build here, or in the worktree's own window?"
  - **Here:** carry on.
  - **Window:** run `code <wt>` and tell the user to type `/resume-handoff` in the new window. Then stop
    working on this slug in this session: two sessions on one worktree collide. The harness does not start
    the new session; the user does.

## Phase 2 — Per-feature implementation

In each worktree, always through `.venv/bin/...`:

1. **Brainstorm** (`superpowers:brainstorming`) if creative/ambiguous.
2. **Register the run.** Save the user's feature text **verbatim** as `request.md` in the
   session scratchpad. Write the plan with its file table first. Start an empty divergence log.
   Then `$G init --repo <wt> --request <request.md> --plan <plan.md> --divergence-log <log.md>`, and give
   the user the dashboard path it prints (the live view of every gate round; `/gate`, Register the run).
3. **Plan gate:** `/gate plan <plan.md>`. In gated mode the PASS is the approval. Otherwise show the
   plan and the verdict, then wait. After one round with findings, fix them, show each fix, and wait for
   the user's `#approve-plan` (no second round; `/gate plan` step 3).
   The passing stamp writes the handoff: ask "build here, or in the worktree's own window?"
   (Handoff + open the window, above).
4. **Implement:** invoke `implement-plan` with

   ```
   plan: .worktrees/<slug>/.handoff/plan.md   workdir: .worktrees/<slug>   reviewer: gate   owns: commit, integration
   ```

   `implement-plan` runs the tripwire, `/gate review` and `/gate checks` itself, using TDD.
5. **Before the commit:**
   - **Interpreter:** `cd .worktrees/<slug> && .venv/bin/python -c "import sys; print(sys.prefix)"`
     must print a path inside the worktree.
   - **No up-front baseline run.** A check that fails gets a lazy baseline,
     `$G checks-run --repo <wt> --baseline <id>`, which says whose failure it is. It still blocks.
   - **Formatting:** never run a formatter over the whole tree. Format only files you created.
     For a file you edited, first check that it was clean at `HEAD` (for example
     `git show HEAD:<file> | .venv/bin/black --check -`); if it was not, leave it.
   - **Lint** is a `[[checks]]` entry when the project wants it; there is no separate lint step.
   - **Stamps:** `$G status --repo <wt>` must show `review` and `checks` stamps for the index tree.
6. **Commit.**
   - **Gated:** `git add <explicit paths>`, then, as a separate command, a plain `git commit`. One
     concept per commit, a compact subject, and the project's identity flags. The hook re-checks the
     stamps and the commit hygiene (DEV_FLOW §5).
   - **Not gated:** stop, summarize, propose the message, and wait.

`parallel-agents` mode: dispatch steps 1–5 inside parallel workers, each in its
own worktree (Claude Code: `Agent` with `isolation: "worktree"`). Use it only for
genuinely independent slugs — one change that spans a function signature and its
call sites is shared state, and splitting it across agents produces conflicts,
not speed. Agents that share a database or other external state collide; check
`## Gotchas`.

## Phase 3 — Push & MR (explicit auth, or a gated request)

Push pushes **commits**, not the working tree. First confirm there is something
to send: an empty `git log origin/<base>..HEAD` means the work is still
uncommitted, and the push would create a remote branch identical to `<base>`.

Then `/gate push`. It audits the branch (hygiene, forbidden paths, secrets, base
freshness) and writes the push stamp; the hook blocks the push without it.

The `<summary>` below is implement-plan's summary followed by `$G summary --repo <wt> --head`:
the reviewer list with reasons, and the **How it was tested** table. End the
session's final message with `$G summary --repo <wt> --tested --head`.

Pick the command by the host in `git remote get-url origin`.

GitLab:

```bash
git push -u -o merge_request.create -o merge_request.target=<target> -o merge_request.title="<title>" -o merge_request.description="<summary>" -o merge_request.remove_source_branch origin feat/<slug>
```

GitHub:

```bash
git push -u origin feat/<slug>
gh pr create --base <target> --head feat/<slug> --title "<title>" --body "<summary>"
```

Report the MR/PR URL. Do not merge. If `## Gotchas` lists expected conflict
files, say how to resolve them.

**Never run a bare `git push` in a feature worktree, and never accept either
branch git suggests when one fails.** `git push origin HEAD:<base>` — git's own
first suggestion — pushes the feature commits straight onto the shared branch,
with no MR and no review. `git push origin HEAD` omits `-u`, so the next push
fails the same way. Always push with an explicit refspec.

Recovery if the fatal shows up anyway:

```bash
git branch --unset-upstream
git push -u origin HEAD:feat/<slug>
```

## Phase 4 — Cleanup (trigger: `clean <slug>`)

`<check>` is the cleanup-check branch from `## Branches`.

```bash
git fetch origin
git diff origin/<check>...feat/<slug>
```

Must be empty. The three dots show only the changes on `feat/<slug>` that
`<check>` does not have yet; two dots would also show everything else that
landed on `<check>` since.

```bash
git worktree add .worktrees/<check>-cleanup <check>
cd .worktrees/<check>-cleanup && git pull --ff-only && git merge-base --is-ancestor feat/<slug> HEAD && git worktree remove ../<slug> && git branch -d feat/<slug>
```

- Skip the `worktree add` if `.worktrees/<check>-cleanup` already exists. If
  `<check>` is already checked out somewhere (often the main checkout),
  `worktree add` fails — run the second line from that checkout instead, with
  `.worktrees/<slug>` as the path.
- Never use `-D`. If the ancestor check fails (for example after a squash
  merge), stop and ask.
- `git worktree remove` deletes `.venv/` with it.
