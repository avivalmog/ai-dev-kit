# Setting up a project

The kit works in any git repo with no setup: the shared reviewers run, plus `pytest -q` when `.py` files
change. Two optional files make it fit a project. A complete example, for an imaginary `orders-service`,
is in [`examples/project/`](../examples/project/).

| file | what it controls | without it |
| --- | --- | --- |
| `.claude/parallel-feature-workflow.md` | how a worktree is made: branches, setup, files to copy or link, gotchas | the defaults below |
| `reviewers/gates.toml` | which extra reviewers and checks run, risky files, limits, budget | the shared defaults |

## 1. The project file: `.claude/parallel-feature-workflow.md`

Read by `parallel-feature-workflow` before it creates a worktree. Each `##` section replaces one default;
leave a section out to keep its default. Example: [`examples/project/.claude/parallel-feature-workflow.md`](../examples/project/.claude/parallel-feature-workflow.md).

| section | default when absent |
| --- | --- |
| `## Branches` | base `main`, MR target `main`, cleanup check `main` |
| `## Pre-flight` | nothing extra |
| `## Copy into worktree` | `.env`, if it exists |
| `## Link from main` | nothing (use it for big dependency folders such as `node_modules`) |
| `## Setup` | `python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"` for a Python project |
| `## Verify` | `.venv/bin/python -m pytest -q`; ignored once `reviewers/gates.toml` exists |
| `## Gotchas` | none (list files that always conflict in MRs, and shared state such as one local database) |

## 2. The gates: `reviewers/gates.toml`

Merged **tighten-only** onto the kit's `core/skills/dev/gate/default-gates.toml`. A project may add
reviewers, checks and risky files, or lower a limit. It can never remove a shared reviewer or loosen a
shared limit. Example: [`examples/project/reviewers/gates.toml`](../examples/project/reviewers/gates.toml).

| key | what it does |
| --- | --- |
| `base` | the branch the diff is measured against |
| `max_usd` | budget for `kind = "real"` checks; `0` skips them and reports them as unverified |
| `doc_maps` | docs that say which rules govern which path (usually `AGENTS.md` or `CLAUDE.md`) |
| `[caps]` | failed attempts per stage before the flow stops and asks: `plan_rounds`, `review_rounds` (counted per reviewer), `check_rounds`, `push_rounds` |
| `[globs] data` | where this repo's stored data and queries live, for the `data-schema` reviewer |
| `[tiers.full] always_when` | risky files: any change to one gets the full tier and the tripwire review |
| `[tests] map` | changed module to its test file, for `{targeted_tests}` in a check |
| `[commit] junk_patterns` | more files that must never be committed |
| `[push] forbid_paths` | more files that must never be pushed |
| `[[reviewer]]` | a project reviewer (below) |
| `[[checks]]` | a project check: `id`, `kind` (`mechanical`, `unit`, `dry-run`, `ui`, `real`), `when` globs, `run`, optional `last = true` for the full suite |

Once a project declares any `[[checks]]`, they replace the shared fallback `pytest -q`.

## 3. Project reviewers

A reviewer guards something only this project has: an API contract, a migration policy, an authoring
guide. Each one is a `[[reviewer]]` table with an `id`, a `stage` (a label, `logic` or `quality`;
every reviewer runs in the same round), `applies_to` globs, and one of three kinds:

| kind | you write | the example |
| --- | --- | --- |
| `agent` | a full reviewer prompt: its lens as a table of finding ids and severities, and what is not a finding | [`reviewers/api-contract.md`](../examples/project/reviewers/api-contract.md) |
| `rules` | nothing new: point it at an existing rules doc; the shared rules template extracts and checks each rule | [`docs/MIGRATIONS.md`](../examples/project/docs/MIGRATIONS.md) |
| `checklist` | a YAML allowlist of rules, for a long doc where you choose which rules count | — |

Optional keys: `tiers` (default `["standard", "full"]`) and `model` (a model tier: `light`, `standard` or
`strong`; default `standard`).

## 4. Calibrating a project reviewer

Calibration proves a reviewer can see what it is meant to see. Give it two small test cases under
`reviewers/calibration/<id>/`:

```
reviewers/calibration/api-contract/
  caught/   request.md  diff.patch  files/…  expected.json   {"id": "AC1", "file": "schemas.py", "line": 7}
  clean/    request.md  diff.patch  files/…  expected.json   {"clean": true}
```

- `caught/` plants one defect. The reviewer passes when it reports that finding id within 3 lines of `line`.
- `clean/` is a correct change. The reviewer passes when it reports no blocking finding.
- `files/` is the code the reviewer reads; `diff.patch` paths are relative to it.

Run `/gate calibrate <id>` after writing the reviewer and after every edit to it or its `model`. The pass
record is kept in the gate state on your machine, so one calibration covers every branch and worktree.
An uncalibrated reviewer still runs, with a warning that its result is unproven.

## 5. Check the setup

`gate.py explain` prints which reviewers and checks a change would get, and why, without running anything:

```bash
uv run --no-project --python 3.12 ~/.claude/skills/gate/gate.py explain --repo . --range main..HEAD
```

A project reviewer that never shows up as `RUN` has `applies_to` globs that match nothing.
