# Planning — the detail behind the rule in the general rules (`AGENTS.md`)

Read this when entering plan mode, before writing the plan.

## File inventory before prose

**Plan mode only.** Outside plan mode (auto mode, or a direct "do X" ask), do
NOT show this table: still do the producer/consumer sweep silently so no reader
is missed, then offer the table in one line ("say the word for the file table").

In plan mode, every plan opens with a table — no narrative above it — then
STOPS for the user's approval before any plan prose is written:

| file | producer or consumer role | exact change | why needed |

One row per file touched, covering BOTH the side that writes the data and
*every* side that reads it (a prompt template AND its renderer; a new output
field AND its serializer AND the validator AND the page that displays it). A
missing consumer row is the failure this table exists to catch — find readers
with `rg`, not memory.
