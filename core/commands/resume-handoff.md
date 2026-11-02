---
description: Resume work in this worktree from its HANDOFF.md
argument-hint: "[path to handoff, default ./HANDOFF.md] [gated]"
---

You are a fresh session picking up work another session set up in this worktree.

1. Read the handoff file in full: `$ARGUMENTS` if it names a file, otherwise `./HANDOFF.md`
   at the repository root. If it does not exist, say so and stop — do not guess
   what the work is.
2. `gate.py` writes that file and rewrites it after every gate command, so it is
   current. Read every file it lists under "Files" in full: the request (the
   user's words), the plan (`.handoff/plan.md`) and the divergence log. Edit those
   copies, never another copy of the plan.
3. Run `$G status --repo .` (the `gate` skill) and check it agrees with the handoff's
   "Where it stands". Give the user the dashboard path the handoff lists.
4. Re-read any rule, doc, or convention the handoff names (AGENTS.md or CLAUDE.md, an
   authoring doc, the commit rules) before you touch related code.
5. Confirm your environment matches the handoff: you are in the worktree it
   names, on the branch it names, and the venv it names resolves inside the
   worktree (`.venv/bin/python -c "import sys; print(sys.prefix)"`).
6. Do the handoff's "Next step". When it says to stop and ask the user, ask
   them and wait. When it says to implement the plan, invoke `implement-plan`
   with the arguments it gives; its divergence log is the one the handoff lists.
   Work the targeted gates; run the full suite only at an authorized commit.

Hard rule: do NOT `git commit`, `push`, `tag`, or open an MR without my explicit
authorization, even when everything is green — stop at each commit gate, show the
evidence, and ask. The one exception: my `/resume-handoff` message says `gated`. Then a passing gate
authorizes the commit, push and MR, as in `DEV_FLOW.md`. A tag still needs me.

Start by summarizing, in a few lines: where the work stands, what the immediate
next step is, and anything in the handoff that looks stale against the current
tree.
