# parallel-feature-workflow — orders-service

Project file for the shared skill `parallel-feature-workflow`. Each section below replaces that skill's
default; leave a section out to keep the default. Commit this file, or list it in `.git/info/exclude` if it
holds things only your machine needs.

## Branches

- Base: `main`.
- MR target: `main`.
- Cleanup check: `main`.

## Pre-flight

- `docker compose ps db` shows the database container running (the integration tests need it).

## Copy into worktree

- `.env`
- `config/local.toml`

## Link from main

- `frontend/node_modules`

## Setup

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
```

## Verify

Ignored once `reviewers/gates.toml` exists: its `[[checks]]` run through `/gate checks` instead.

```bash
.venv/bin/python -m pytest -q tests/unit
```

## Gotchas

- Two worktrees share the one local database; never run the migration tests in two slugs at once.
- `CHANGELOG.md` conflicts on almost every MR: keep both sides, newest entry first.
