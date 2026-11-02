# ai-dev-kit

A shared coding-agent setup: the rules, the gate reviewers, the general skills and commands. Everything here
works for any project. Project-specific skills stay in their own repos.

> **Start here: read [`docs/prompt-to-mr.html`](docs/prompt-to-mr.html) before using the kit.** It walks
> through every step from the prompt you type to an open merge request, and the rule behind each step, so you
> know what the agent and the gates will do. GitHub shows `.html` files as code, so open it in a browser after
> cloning.

## Rules: two files

| file | what it holds | in Claude Code it becomes |
| --- | --- | --- |
| [`core/rules/CLAUDE.md`](core/rules/CLAUDE.md) | the `CLAUDE.md`: it loads the general rules, then adds personal preferences | `~/.claude/CLAUDE.md` |
| [`core/rules/AGENTS.md`](core/rules/AGENTS.md) | the general rules (git, gates, testing, verification, planning), with no personal preferences | loaded by `CLAUDE.md` |

To change a preference, edit `core/rules/CLAUDE.md`. To change a general rule, edit `core/rules/AGENTS.md`.

## How the kit is organized

The kit has two layers:

- **`core/`** is agent-neutral, plain Markdown plus the Python gate engine. Any coding agent (Claude Code, Codex,
  …) can read and follow it. It names no agent's tools, models or folders.
- **`adapters/<agent>/`** installs the core where one agent looks for it, in that agent's format, and adds what
  only that agent supports (hooks, a status line). Today there is one: `adapters/claude/`.

## What's in the core

| path | what it is |
| --- | --- |
| `core/rules/CLAUDE.md` | the `CLAUDE.md`: loads `AGENTS.md`, then the personal preferences |
| `core/rules/AGENTS.md` | the general rules for every project |
| `core/rules/DEV_FLOW.md` | the gated flow from prompt to MR |
| `core/rules/docs/` | rules read on demand (planning, testing); `AGENTS.md` points at them as `docs/` beside it |
| `core/reviewers/<id>.md` | the gate reviewers: a `name` / `description` header, then the reviewer's instructions |
| `core/skills/<topic>/<name>/` | gate, implement-plan, parallel-feature-workflow, rules-review (`SKILL.md` each) |
| `core/commands/` | `productionize`, `throwaway`, `resume-handoff` prompts; `$ARGUMENTS` is the user's text |

Skills and reviewers are grouped by topic folder. Names must be unique across topics.

**How any agent runs a gate review.** `gate.py` (in the `gate` skill) picks the reviewers and writes one prompt
file per reviewer. Each dispatch entry has an `agent_name`, a `model_tier` (`light`, `standard` or `strong`;
the agent maps it to one of its models) and a `fallback_prompt` that carries the reviewer's own instructions.
An agent with no named reviewer agents starts a fresh read-only worker on the fallback prompt and saves the
reply to `out`. The gate skill's `SKILL.md` has the steps. Gate state lives in `~/.ai-kit/gate-state/`.

To set up a project (its worktree recipe, extra reviewers and checks), see
[`docs/project-setup.md`](docs/project-setup.md) and the worked example in [`examples/project/`](examples/project/).

## Claude Code

### Requirements

- Linux, macOS or WSL. Native Windows is not supported: the kit uses symlinks and POSIX shell.
- `git`, a system `python3` 3.10 or newer (the hooks and the status line run on it), and
  [`uv`](https://docs.astral.sh/uv/) on `PATH` (the gate engine runs on Python 3.12 through uv, which
  downloads it if needed).
- Claude Code.

### Install

1. Clone the kit somewhere it can stay. The links point at this folder, so moving it later breaks them.

   ```bash
   git clone https://github.com/avivalmog/ai-dev-kit.git ~/ai-kit
   ```

2. See what would change. Nothing is written.

   ```bash
   ~/ai-kit/adapters/claude/install.sh --dry-run
   ```

   A file you already have with the same name (your own `CLAUDE.md`, an agent, a skill) shows as `skip`.

3. Install. Add `--force` to replace the files reported as `skip`; each one is moved to
   `~/.claude/kit-backups/<time>/` first, so nothing is lost.

   ```bash
   ~/ai-kit/adapters/claude/install.sh
   ```

4. Turn on the hooks and the status line by adding this to `~/.claude/settings.json` (merge it into any
   `hooks` you already have). The install never edits that file.

   ```json
   {
     "hooks": {
       "PreToolUse": [{"matcher": "Bash|Write|Edit|NotebookEdit",
                       "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/gate_guard.py"}]}],
       "UserPromptSubmit": [{"matcher": "",
                             "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/gate_skip.py"}]}]
     },
     "statusLine": {"type": "command", "command": "python3 ~/.claude/skills/gate/gate_view.py statusline"}
   }
   ```

5. Start a new Claude Code session; skills, agents, hooks and rules are read at startup. Then try it on a
   small feature:
   `/parallel-feature-workflow` with a `slug:` and a `feature:` line
   ([`docs/prompt-to-mr.html`](docs/prompt-to-mr.html) walks through what happens).

**If you used the gate before the kit existed:** the gate keeps its records (which changes passed review and
checks, attempt counts) in a folder. That folder moved from `~/.claude/gate-state` to `~/.ai-kit/gate-state`.
A branch you were in the middle of gating would look unchecked after the install, so move the records once:
`mkdir -p ~/.ai-kit/gate-state && mv ~/.claude/gate-state/repos ~/.ai-kit/gate-state/`. With nothing in
progress you can skip this.

### What the hooks do

The hooks only act on the agent. Your own terminal is never gated.

- **`gate_guard.py`** runs before every Bash, Write, Edit and NotebookEdit call. It blocks a `git commit`
  without passing `review` and `checks` stamps for the exact staged tree, a push or MR without the `push`
  stamp, commit-hygiene failures, wildcard staging (`git add -A`, `.`, `-u`, a directory), `git commit -a`,
  and git commands combined or hidden behind a wrapper. It also stops the agent from writing gate stamps
  or tag files.
- **`gate_skip.py`** reads your typed prompt and arms a tag you wrote in it (`#skip-review`, `#skip-gates`,
  `#accept-gate`, `#approve-plan`) for 2 hours. Only your own prompt can arm a tag.

To work without the gates for a while, remove the two hooks from `settings.json`; everything else keeps
working.

### Updating

`git pull` in the kit updates every linked file at once. Re-run `install.sh` when the pull adds or removes a
file, or changes a reviewer (reviewer files are built copies, not links).

### Uninstalling

Remove the links that point into the kit and the reviewer files it built, then restore anything you want
from `~/.claude/kit-backups/`, and take the hooks out of `settings.json`.

```bash
KIT=~/ai-kit
find ~/.claude -maxdepth 2 -type l -lname "$(cd "$KIT" && pwd)/*" -print -delete
grep -l '^# generated by ai-kit from' ~/.claude/agents/review-*.md | xargs -r rm -v
```

### How the install maps the kit

| from the kit | to `~/.claude/` | how |
| --- | --- | --- |
| `core/rules/CLAUDE.md` | `CLAUDE.md` | link |
| `core/rules/DEV_FLOW.md`, `core/rules/docs/` | `DEV_FLOW.md`, `docs/` | link |
| `core/reviewers/<id>.md` | `agents/review-<id>.md` | **built**: Claude's header (`tools: Read, Grep, Glob`) + the core text |
| `core/skills/*/<name>/` | `skills/<name>/` | link |
| `core/commands/*.md` | `commands/` | link |
| `adapters/claude/hooks/*.py` | `hooks/` | link |

Links mean editing a file in `~/.claude` edits this repo, and `git pull` updates the device. The built
reviewer files are copies: edit `core/reviewers/<id>.md`, never `~/.claude/agents/review-<id>.md`, then re-run
`install.sh`.

`install.sh` is safe to re-run. It never touches an existing file it did not make unless you pass `--force`;
then the old one moves to `~/.claude/kit-backups/<time>/`. It also removes links and built reviewers whose
source was deleted from this repo. Re-run it after a `git pull` that adds or removes files, or changes a
reviewer.

Mapping the model tiers: `light` = `haiku`, `standard` = `sonnet`, `strong` = `opus` (in the gate skill's
dispatch steps, which pass the model on every call).

Kept per device, never committed: `~/.claude/settings.json`, `.credentials.json`, memory (`projects/`),
sessions, plans and history, and `~/.ai-kit/gate-state/`.

## Working on the kit

The rules for changing this repository itself are in [`AGENTS.md`](AGENTS.md) at the root (`CLAUDE.md`
imports it).

## Tests

```bash
uv run --no-project --python 3.12 python -m unittest discover -s core/skills/dev/gate -p 'test_*.py'
python3 -m unittest discover -s ~/.claude/hooks -p 'test_*.py'    # after install: the hooks import the engine from there
```

After editing a reviewer, the dispatch template, the rules template or the output contract, run
`/gate calibrate`: the gate warns about a reviewer whose files or model changed since it was last calibrated.

## License

MIT, see [`LICENSE`](LICENSE). Some reviewer ideas are adapted from another MIT project; the credit is in
[`core/skills/dev/gate/THIRD_PARTY.md`](core/skills/dev/gate/THIRD_PARTY.md).
