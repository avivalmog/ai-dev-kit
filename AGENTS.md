# Working on ai-dev-kit

Rules for changing this repository itself, for any coding agent. The rules the kit installs for every
project are in `core/rules/AGENTS.md`; they apply here too.

## Layout

- `core/` is agent-neutral: rules, reviewers, skills, commands and the gate engine. Nothing in it names one
  agent's tools, models or folders, except inside a paragraph labelled **Claude Code** (or another agent).
- `adapters/claude/` installs the core into `~/.claude`: `install.sh` and the hooks.
- `core/rules/CLAUDE.md` holds the personal preferences and loads `core/rules/AGENTS.md`, the general
  rules. A rule that is not a personal preference goes in `AGENTS.md`.
- `docs/` and `examples/` are for people reading the kit; nothing installs them.

## When you change something

| you change | then also |
| --- | --- |
| a reviewer (`core/reviewers/<id>.md`) | re-run `adapters/claude/install.sh` (reviewer files in `~/.claude/agents/` are built copies), then `/gate calibrate <id>` |
| `dispatch-prompt.md`, `output-contract.md`, or the part of `rules-reviewer-prompt.md` below its `---` line | `/gate calibrate` for every reviewer: these are part of every reviewer's calibration hash |
| a reviewer's `model` in `default-gates.toml` | `/gate calibrate <id>` |
| a reviewer's calibration fixtures | `/gate calibrate <id>` |
| gate behaviour (`gate.py`, `SKILL.md`, `default-gates.toml`) | update `core/rules/DEV_FLOW.md` and `docs/prompt-to-mr.html` if what a user sees changed |
| `install.sh` | test it against a scratch home first: `HOME=<scratch dir> adapters/claude/install.sh`, never the real `~/.claude` first |
| anything a user installs or configures | update `README.md`, and `docs/project-setup.md` for project-level files |

Never edit `~/.claude/agents/review-*.md` directly; they are rebuilt from `core/reviewers/` on every install.

## Code constraints

- `gate.py` runs under Python 3.12 through `uv`. `gate_common.py`, `gate_view.py` and the hooks run under the
  system `python3` and must stay 3.10-compatible and stdlib-only.
- If `gate_guard.py` crashes, it still refuses gated git commands (commit, push, MR) and lets every other
  tool call through, so a bug never opens the gate and never locks the whole session. Keep it that way.

## Tests

```bash
uv run --no-project --python 3.12 python -m unittest discover -s core/skills/dev/gate -p 'test_*.py'
python3 -m unittest discover -s adapters/claude/hooks -p 'test_*.py'
```

The hook tests import the engine through `~/.claude/skills/gate`, so they test the installed kit.
