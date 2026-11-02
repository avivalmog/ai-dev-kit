# Testing — the detail behind the rule in the general rules (`AGENTS.md`)

Read this before running more than one test file, and always before a full suite.

## Targeted tests by default; full suite ONLY at the commit gate

Run the narrowest selection that covers the change — the specific test file(s)
touching it. Run the FULL suite only:

  - in the same turn as an authorized commit (the pre-commit gate), or
  - after a structural CODE change: a moved or renamed module, a change to a
    schema / model *class*, an import-graph change, or anything whose blast
    radius I cannot name up front.

**Editing data is NOT a structural change.** A `data/**.yaml` / config / fixture
/ prompt / docs edit — adding, moving, renaming or deleting registry rows,
taxonomy entries, scope lists — gets the targeted test files, never the full
suite, however "core" the file feels. "Schema / model change" above means Python
schema/model CODE, not rows in a YAML registry.

**Before reaching for a full run, name the blast radius in one line.** If a grep
shows the changed values are referenced only in the file I edited plus a few
named tests, that IS naming it — run those files and stop. "I am not certain" is
not the same as "I cannot name it": grep first, then decide.

Never re-run a passing full suite to confirm a small follow-up edit, never run it
twice in one turn, and never run it speculatively while waiting for commit
approval. When a full run IS warranted, add `--durations=15` so the time buys
something beyond a pass/fail.

**Why:** on a large suite the full run is minutes and a single file is seconds.
A 10-line taxonomy sub-threat move once cost a 4-minute full run when the 9
relevant test files passed in 9 seconds — and the blast radius had already been
grepped and proven to be one file. The targeted files prove the change; the
single pre-commit run catches anything they missed.
