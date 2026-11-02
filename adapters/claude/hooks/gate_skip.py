#!/usr/bin/env python3
"""UserPromptSubmit hook: the user's #skip-review, #skip-gates, #accept-gate, #approve-plan tags arm gate tokens.

The harness also relays subagent reports and task notifications through this hook, and a reviewer may
quote a tag, so a prompt carrying any RELAY_MARKERS never arms a token: only text the user typed does.
Tokens expire after TOKEN_TTL_S; the guard (gate_guard.py) and gate.py consume them. Flow: ~/.claude/DEV_FLOW.md.
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "gate"))
from gate_common import (TOKEN_ACCEPT, TOKEN_APPROVE_PLAN, TOKEN_SKIP_GATES, TOKEN_SKIP_REVIEW,  # noqa: E402
                         TOKEN_TTL_H, TOKENS_DIR)

TOKEN_PROMPT_MAX_CHARS = 200         # how much of the arming prompt the token records
RELAY_MARKERS = ("<agent-message", "[Subagent hand-back]", "<task-notification", "<system-reminder",
                 "<cross-session-message", "SYSTEM NOTIFICATION")
TAGS = {
    "#skip-review": (TOKEN_SKIP_REVIEW, "the next commit and push skip the reviewer rounds; checks still run"),
    "#skip-gates": (TOKEN_SKIP_GATES, "the next commit and push skip review, checks and push stamps; "
                                      "hygiene and the secrets audit still run"),
    "#accept-gate": (TOKEN_ACCEPT, "the gate stopped at its cap may be stamped ACCEPTED (`stamp --accept`)"),
    "#approve-plan": (TOKEN_APPROVE_PLAN, "the plan, fixed after its review, may be stamped APPROVED "
                                          "with no re-review (`stamp --stage plan --approve`)"),
}


def main() -> None:
    """Arm a token per tag found in the prompt and tell the agent what was armed."""
    try:
        data = json.load(sys.stdin)
    except ValueError:
        sys.exit(0)
    prompt, session = str(data.get("prompt", "")), str(data.get("session_id", ""))
    if any(marker in prompt for marker in RELAY_MARKERS):
        sys.exit(0)          # relayed agent or harness text, not something the user typed
    armed = []
    for tag, (kind, effect) in TAGS.items():
        if re.search(rf"(?<![\w-]){re.escape(tag)}(?![\w-])", prompt):
            TOKENS_DIR.mkdir(parents=True, exist_ok=True)
            (TOKENS_DIR / f"{kind}.json").write_text(json.dumps(
                {"kind": kind, "session": session, "created": time.time(), "prompt": prompt[:TOKEN_PROMPT_MAX_CHARS]}))
            armed.append(f"{tag}: {effect}")
    if armed:
        print(f"gate: the user armed (one-shot, expires in {TOKEN_TTL_H}h) " + "; ".join(armed))
    sys.exit(0)


if __name__ == "__main__":
    main()
