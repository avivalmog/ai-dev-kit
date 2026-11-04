"""Constants and the state layout shared by gate.py and the two hooks, so the three can never disagree.

Python 3.10-compatible on purpose: the hooks run under the system /usr/bin/python3, gate.py under uv's 3.12.
The state lives in ~/.ai-kit/gate-state, outside any one agent's folder. GATE_STATE_DIR in the environment
moves it (tests use it); the hooks only ever see their agent's own environment, so a tool call cannot redirect
them.
"""
import hashlib
import os
from pathlib import Path

STATE_DIR_NAME = "gate-state"                # the guard protects any path containing it
STATE_DIR = Path(os.environ.get("GATE_STATE_DIR") or Path.home() / ".ai-kit" / STATE_DIR_NAME)
TOKENS_DIR = STATE_DIR / "tokens"
TOKEN_TTL_H = 2
TOKEN_TTL_S = TOKEN_TTL_H * 60 * 60            # a user's #skip-* / #accept-gate token expires after this
TOKEN_SKIP_REVIEW, TOKEN_SKIP_GATES, TOKEN_ACCEPT = "skip-review", "skip-gates", "accept-gate"
TOKEN_APPROVE_PLAN = "approve-plan"         # the user approved the agent's fix of reviewed plan findings
VERDICT_PASS, VERDICT_ACCEPTED, VERDICT_APPROVED = "PASS", "ACCEPTED", "APPROVED"
PASSING = (VERDICT_PASS, VERDICT_ACCEPTED, VERDICT_APPROVED)
CANARY_OK = ("caught", "unavailable")       # a round counts only when its canary was caught (or had none)
BLOCKING = ("blocker", "should-fix")        # real defects stop the gate; a nit is only listed
STATUS_PASS, STATUS_FAIL, STATUS_SKIPPED = "pass", "fail", "skipped"   # a check's result
TREE_INDEX, TREE_HEAD = "index", "head"      # the trees the view reads: what a commit records, what a push sends
AUDIT_STAGED, AUDIT_BRANCH = "staged", "branch"
STAGE_PLAN, STAGE_REVIEW, STAGE_CHECKS, STAGE_PUSH = "plan", "review", "checks", "push"   # also stamp file names
COMMIT_STAGES = (STAGE_REVIEW, STAGE_CHECKS)
PUSH_STAGES = (STAGE_REVIEW, STAGE_CHECKS, STAGE_PUSH)
SKIP_WAIVES = {TOKEN_SKIP_GATES: COMMIT_STAGES + (STAGE_PUSH,), TOKEN_SKIP_REVIEW: (STAGE_REVIEW,)}
USER_SKIP_FILE = "user-skip.json"
REPO_ID_HASH_LEN = 8
TREE_SHORT_LEN = 10                           # a tree id as messages print it


def repo_id(common_dir: str) -> str:
    """Stable id of a repository: its main checkout's folder name plus a hash of its common git dir."""
    return f"{Path(common_dir).parent.name}-{hashlib.sha1(common_dir.encode()).hexdigest()[:REPO_ID_HASH_LEN]}"


def repo_state(common_dir: str, root: Path | None = None) -> Path:
    """State root of one repository; every worktree of it shares this directory."""
    return (root or STATE_DIR) / "repos" / repo_id(common_dir)


def tree_state(common_dir: str, tree: str, root: Path | None = None) -> Path:
    """Stamps and logs for one tree of one repository."""
    return repo_state(common_dir, root) / "trees" / tree
