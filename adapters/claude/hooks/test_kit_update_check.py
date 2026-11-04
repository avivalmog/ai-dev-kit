"""Tests for kit_update_check.py, run as a real hook process against a scratch kit clone and its scratch origin.

Run: /usr/bin/python3 -m unittest discover -s ~/.claude/hooks -p 'test_*.py'
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from test_gate_hooks import git

HOOK = Path(__file__).resolve().parent / "kit_update_check.py"
HANG_S = 10         # a remote that answers later than the hook is willing to wait


def clone(origin: Path, dest: Path) -> None:
    """Clone a scratch origin."""
    subprocess.run(["git", "clone", "-q", str(origin), str(dest)], check=True, capture_output=True)


class UpdateCheckCase(unittest.TestCase):
    """A bare `origin`, a `writer` clone that publishes to it, and a `my kit` clone holding the hook."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.origin, self.writer, self.kit = self.root / "origin.git", self.root / "writer", self.root / "my kit"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        clone(self.origin, self.writer)
        self.publish("base")
        clone(self.origin, self.kit)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def publish(self, message: str) -> None:
        git(self.writer, "commit", "-q", "--allow-empty", "-m", message)
        git(self.writer, "push", "-q", "origin", "HEAD")

    def check(self, kit: Path | None = None, **env: str) -> str:
        """Run the hook from `kit`, as the link install.sh makes would resolve, and return its stdout."""
        hook = (kit or self.kit) / "adapters" / "claude" / "hooks" / HOOK.name
        hook.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(HOOK, hook)
        done = subprocess.run(["/usr/bin/python3", str(hook)], input="{}", capture_output=True, text=True,
                              env=dict(os.environ, **env))
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def assert_notified(self, out: str) -> None:
        update = f"cd '{self.kit}' && git pull && adapters/claude/install.sh"
        notice = json.loads(out)
        self.assertIn(update, notice["systemMessage"])
        self.assertEqual(notice["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn(update, notice["hookSpecificOutput"]["additionalContext"])

    def test_same_commit_is_silent(self) -> None:
        self.assertEqual(self.check(), "")

    def test_commit_never_fetched_notifies_until_pulled(self) -> None:
        self.publish("newer")
        self.assert_notified(self.check())
        git(self.kit, "pull", "-q")
        self.assertEqual(self.check(), "")

    def test_commit_fetched_but_not_merged_notifies(self) -> None:
        self.publish("newer")
        git(self.kit, "fetch", "-q")
        self.assert_notified(self.check())

    def test_local_commits_ahead_is_silent(self) -> None:
        git(self.kit, "commit", "-q", "--allow-empty", "-m", "local work")
        self.assertEqual(self.check(), "")

    def test_branch_behind_the_default_branch_notifies(self) -> None:
        git(self.kit, "checkout", "-q", "-b", "feat/x")
        self.publish("newer")
        self.assert_notified(self.check())

    def test_unreachable_remote_is_silent(self) -> None:
        git(self.kit, "remote", "set-url", "origin", str(self.root / "missing.git"))
        self.assertEqual(self.check(), "")

    def test_hanging_remote_gives_up_silently(self) -> None:
        git(self.kit, "remote", "set-url", "origin", "ssh://kit.invalid/kit.git")
        started = time.monotonic()
        self.assertEqual(self.check(GIT_SSH_COMMAND=f"sh -c 'sleep {HANG_S}'"), "")
        self.assertLess(time.monotonic() - started, HANG_S)

    def test_folder_that_is_not_a_clone_is_silent(self) -> None:
        self.assertEqual(self.check(self.root / "unpacked"), "")


if __name__ == "__main__":
    unittest.main()
