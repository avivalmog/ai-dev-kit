# Rules for Claude Code

## General rules

The rules that are not personal preferences live in their own file, `AGENTS.md`, beside this one:

@AGENTS.md

## Personal preferences

### HARD RULE — short answer first, detail only on request

Lead with the answer itself, in 1-3 sentences. For a validation question ("is X
true in this codebase?") that means the verdict plus the one `file:line` that
proves it — then STOP. No walkthrough, no call-path narration, no code dump, no
summary of what I checked; offer depth in one short line ("say the word for the
call path").

**Why:** a long answer buries the fact and I stop reading it.

**Finished work gets the same rule.** The last message is a short summary in
simple English: what changed and what the user must know, in a few sentences,
with the proof as the KIND of check and its result (e.g. "Unit tests: 12
passed. Dry run: 40 prompts rendered, no errors."). Say "unverified" for what
was not checked. Then list other detail as titles only ("Full test output",
"Changed files") and ask which to show.

Full explanation is still correct when I ask "why" / "how", or for a plan.
