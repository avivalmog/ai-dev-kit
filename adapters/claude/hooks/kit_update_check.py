#!/usr/bin/env python3
"""SessionStart hook: tell the user when GitHub has a newer kit than the one installed.

The installed hooks are links into the kit's clone, so the clone's HEAD is the installed version. Any failure
(offline, no clone, no remote) prints nothing: a version check never gets in the way of starting a session.
"""
import json
import shlex
import subprocess
from pathlib import Path

KIT = Path(__file__).resolve().parents[3]   # this file is <kit>/adapters/claude/hooks/, reached through a link
GIT_TIMEOUT_S = 5                           # the longest a new session waits on the network


def git(*args: str) -> subprocess.CompletedProcess:
    """Run git in the kit. With no controlling terminal, git and ssh fail instead of asking for credentials."""
    return subprocess.run(["git", "-C", str(KIT), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
                          start_new_session=True)


def is_behind() -> bool:
    """True when origin's default branch has a commit HEAD lacks; False when up to date or the check failed."""
    try:
        remote = git("ls-remote", "origin", "HEAD").stdout.split()
        # A tip this clone never fetched makes --is-ancestor fail too, and it is just as new.
        return bool(remote) and git("merge-base", "--is-ancestor", remote[0], "HEAD").returncode != 0
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> None:
    """Print the notice as hook JSON when the kit is behind, nothing otherwise."""
    if not is_behind():
        return
    update = f"cd {shlex.quote(str(KIT))} && git pull && adapters/claude/install.sh"
    print(json.dumps({
        "systemMessage": f"ai-dev-kit: a newer version is on GitHub. Update with: {update}",
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "A newer ai-dev-kit is on GitHub. Tell the user once, at the start of your first "
                                 f"reply, to run: {update} . Do not run it yourself.",
        },
    }))


if __name__ == "__main__":
    main()
