"""Tests for gate_guard.py and gate_skip.py, run as real hook processes against scratch repos.

State and tokens go to a temp GATE_STATE_DIR, so the real ones are never touched.
Run: /usr/bin/python3 -m unittest discover -s ~/.claude/hooks -p 'test_*.py'
"""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
SESSION = "hooktest-session"


def git(repo: Path, *args: str) -> str:
    """Run git in a test repo."""
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class HookCase(unittest.TestCase):
    """A repo with `main` and a checked-out `feat/x`, and a private gate state dir."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "state"
        self.env = dict(os.environ, GATE_STATE_DIR=str(self.state))
        self.repo = root / "r"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("A = 1\n")
        git(self.repo, "add", "src/a.py")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "checkout", "-q", "-b", "feat/x")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def hook(self, name: str, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(["/usr/bin/python3", str(HOOKS / name)], input=json.dumps(payload),
                              capture_output=True, text=True, env=self.env)

    def bash(self, command: str, cwd: Path | None = None) -> int:
        return self.hook("gate_guard.py", {"tool_name": "Bash", "tool_input": {"command": command},
                                           "cwd": str(cwd or self.repo), "session_id": SESSION}).returncode

    def write(self, path: str) -> int:
        return self.hook("gate_guard.py", {"tool_name": "Write", "tool_input": {"file_path": path},
                                           "cwd": str(self.repo), "session_id": SESSION}).returncode

    def arm(self, prompt: str, session: str = SESSION) -> subprocess.CompletedProcess:
        return self.hook("gate_skip.py", {"prompt": prompt, "session_id": session})

    def tree_dir(self, tree: str) -> Path:
        common = git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        rid = f"{Path(common).parent.name}-{hashlib.sha1(common.encode()).hexdigest()[:8]}"
        return self.state / "repos" / rid / "trees" / tree

    def stamp(self, tree: str, *stages: str) -> None:
        d = self.tree_dir(tree)
        d.mkdir(parents=True, exist_ok=True)
        for s in stages:
            (d / f"{s}.json").write_text(json.dumps({"verdict": "PASS"}))


class StagingTest(HookCase):
    def test_wildcards_and_directories_blocked(self) -> None:
        (self.repo / "src" / "a.py").write_text("A = 2\n")
        for cmd in ("git add .", "git add -A", "git add src", "git add -u", "git add --all"):
            self.assertEqual(self.bash(cmd), 2, cmd)
        self.assertEqual(self.bash("git add src/a.py"), 0)
        self.assertEqual(self.bash(f"R={self.repo}; cd $R && git add src/a.py", self.repo.parent), 0)
        self.assertEqual(self.bash(f"R={self.repo}; cd ${{R}} && git add src", self.repo.parent), 2)


class CommitTest(HookCase):
    def setUp(self) -> None:
        super().setUp()
        (self.repo / "src" / "a.py").write_text("A = 2\n")
        git(self.repo, "add", "src/a.py")
        self.tree = git(self.repo, "write-tree")

    def test_tree_changing_forms_blocked(self) -> None:
        self.stamp(self.tree, "review", "checks")
        for cmd in ('git commit -am "x"', 'git commit -a -m "x"', 'git commit --all -m "x"',
                    'git commit -m "x" src/a.py', 'git commit -m "x" -- src/a.py',
                    'git add src/a.py && git commit -m "x"'):
            self.assertEqual(self.bash(cmd), 2, cmd)

    def test_needs_both_stamps(self) -> None:
        self.assertEqual(self.bash('git commit -m "x"'), 2)
        self.stamp(self.tree, "review")
        self.assertEqual(self.bash('git commit -m "x"'), 2)
        self.stamp(self.tree, "checks")
        self.assertEqual(self.bash('git commit -m "x"'), 0)
        self.assertEqual(self.bash('git -c user.name=t commit -F msg.txt'), 0)
        self.assertEqual(self.bash(f'cd {self.repo} && git commit -m "x"', self.repo.parent), 0)

    def test_skip_review_waives_only_review(self) -> None:
        self.stamp(self.tree, "checks")
        self.arm("fix the typo #skip-review")
        self.assertEqual(self.bash('git commit -m "x"'), 0)
        self.assertEqual(json.loads((self.tree_dir(self.tree) / "user-skip.json").read_text())["kind"], "skip-review")
        self.assertFalse(any((self.state / "tokens").glob("*.json")))

    def test_shell_keywords_do_not_hide_a_commit(self) -> None:
        cmd = 'if ! git diff --cached --quiet; then git commit -m "x"; fi'
        self.assertEqual(self.bash(cmd), 2)
        self.assertEqual(self.bash('{ git commit -m "x"; }'), 2)
        self.stamp(self.tree, "review", "checks")
        self.assertEqual(self.bash(cmd), 0)

    def test_any_index_change_before_a_commit_refused(self) -> None:
        self.stamp(self.tree, "review", "checks")
        for cmd in ('git rm -q --cached src/a.py && git commit -m "y"', 'git reset src/a.py; git commit -m "y"',
                    'git restore --staged src/a.py && git commit -m "y"', 'git commit --pathspec-from-file=p.txt -m y'):
            self.assertEqual(self.bash(cmd), 2, cmd)

    def test_a_bare_redirection_does_not_crash_the_guard(self) -> None:
        self.assertEqual(self.bash("> build.log; git add -A"), 2)
        self.assertEqual(self.bash("> build.log; git status"), 0)

    def test_redirections_are_not_pathspecs(self) -> None:
        self.stamp(self.tree, "review", "checks")
        self.assertEqual(self.bash('git commit -m "x" 2>&1'), 0)
        self.assertEqual(self.bash('git commit -m "x" > /dev/null 2>&1'), 0)

    def test_skip_token_survives_a_blocked_commit(self) -> None:
        (self.repo / "build.log").write_text("junk\n")
        git(self.repo, "add", "build.log")
        tree = git(self.repo, "write-tree")
        self.stamp(tree, "checks")
        self.arm("#skip-review")
        self.assertEqual(self.bash('git commit -m "x"'), 2)          # H2 junk blocks the commit
        self.assertTrue((self.state / "tokens" / "skip-review.json").exists())
        git(self.repo, "rm", "-q", "--cached", "build.log")
        self.stamp(self.tree, "checks")
        self.assertEqual(self.bash('git commit -m "x"'), 0)          # the retry still has the user's skip
        self.assertFalse((self.state / "tokens" / "skip-review.json").exists())

    def test_skip_gates_waives_every_stamp(self) -> None:
        self.arm("#skip-gates")
        self.assertEqual(self.bash('git commit -m "x"'), 0)
        self.assertEqual(json.loads((self.tree_dir(self.tree) / "user-skip.json").read_text())["kind"], "skip-gates")

    def test_expired_token_ignored(self) -> None:
        self.arm("#skip-gates")
        token = self.state / "tokens" / "skip-gates.json"
        data = json.loads(token.read_text())
        data["created"] = 0                                          # armed at the epoch: long past the TTL
        token.write_text(json.dumps(data))
        self.assertEqual(self.bash('git commit -m "x"'), 2)

    def test_newer_skip_gates_supersedes_a_recorded_skip_review(self) -> None:
        self.stamp(self.tree, "checks")
        self.arm("#skip-review")
        self.assertEqual(self.bash('git commit -m "x"'), 0)          # records skip-review for the tree
        git(self.repo, "commit", "-q", "-m", "x")
        self.assertEqual(self.bash("git push"), 2)                   # review waived, checks stamped, push missing
        self.arm("#skip-gates")
        self.assertEqual(self.bash("git push"), 0)
        self.assertFalse((self.state / "tokens" / "skip-gates.json").exists())

    def test_token_from_another_session_ignored(self) -> None:
        self.arm("#skip-gates", session="other-session")
        self.assertEqual(self.bash('git commit -m "x"'), 2)

    def test_unparseable_git_command_blocked(self) -> None:
        self.assertEqual(self.bash('git commit -m "unterminated'), 2)


class MultiLineTest(HookCase):
    def setUp(self) -> None:
        super().setUp()
        (self.repo / "src" / "a.py").write_text("A = 2\n")
        git(self.repo, "add", "src/a.py")
        self.tree = git(self.repo, "write-tree")

    def test_every_line_is_guarded(self) -> None:
        for cmd in (f"cd {self.repo}\ngit commit -m x", "git add src/a.py\ngit commit -m x", "git status\ngit push",
                    "git log\ngit add -A", "git status # look first\ngit push", "true;(git push)",
                    "git log | head\n\ngit commit -m x"):
            self.assertEqual(self.bash(cmd), 2, repr(cmd))

    def test_heredoc_bodies_comments_and_here_strings_are_not_commands(self) -> None:
        self.assertEqual(self.bash("cat <<'EOF' > notes.md\ngit push\ngit add -A\nEOF\necho done"), 0)
        self.assertEqual(self.bash("# git push\nls"), 0)
        self.assertEqual(self.bash('cat <<< "git push"'), 0)
        self.assertEqual(self.bash("cat <<'EOF' > notes.md\ntext\nEOF\ngit push"), 2)
        self.assertEqual(self.bash("cat <<'EOF'\ngit push"), 2)               # the heredoc never ends: fail closed

    def test_multi_line_and_glued_forms_of_a_plain_commit_pass(self) -> None:
        self.stamp(self.tree, "review", "checks")
        for cmd in ('git commit \\\n  -m "x"', 'git commit -m "subject\n\nbody line"',
                    'git commit -m"docs: fix typo"', 'git commit -m ""', "git commit -uno -m x"):
            self.assertEqual(self.bash(cmd), 0, repr(cmd))
        self.assertEqual(self.bash('git commit -m "" -a'), 2)


class FailClosedTest(HookCase):
    def test_missing_engine_blocks_only_gated_git(self) -> None:
        self.env["HOME"] = str(Path(self.tmp.name) / "no-home")   # gate_common is not importable from here
        self.assertEqual(self.bash('git commit -m "x"'), 2)
        self.assertEqual(self.bash("git push"), 2)
        self.assertEqual(self.bash("ls -la"), 0)
        self.assertEqual(self.bash("git status"), 0)


class PushTest(HookCase):
    def setUp(self) -> None:
        super().setUp()
        (self.repo / "src" / "a.py").write_text("A = 2\n")
        git(self.repo, "add", "src/a.py")
        git(self.repo, "commit", "-q", "-m", "x")

    def head_tree(self) -> str:
        return git(self.repo, "rev-parse", "HEAD^{tree}")

    def test_push_and_mr_need_every_stamp(self) -> None:
        self.assertEqual(self.bash("git push -u origin feat/x"), 2)
        self.assertEqual(self.bash("glab mr create --fill"), 2)
        self.stamp(self.head_tree(), "review", "checks", "push")
        self.assertEqual(self.bash("git push -u origin feat/x"), 0)

    def test_push_in_the_same_call_as_a_commit_refused(self) -> None:
        self.stamp(self.head_tree(), "review", "checks", "push")     # the previous HEAD is fully stamped
        (self.repo / "src" / "a.py").write_text("A = 3\n")
        git(self.repo, "add", "src/a.py")
        self.stamp(git(self.repo, "write-tree"), "review", "checks")
        self.assertEqual(self.bash('git commit -m "y" && git push'), 2)
        self.assertEqual(self.bash('git commit -m "y"; glab mr create --fill'), 2)
        self.assertEqual(self.bash('git commit -m "y"'), 0)

    def test_wrapped_git_refused(self) -> None:
        self.stamp(self.head_tree(), "review", "checks", "push")
        for cmd in ("timeout 120 git push", "env GIT_TRACE=1 git push", "nohup git commit -m x",
                    "sudo glab mr create --fill"):
            self.assertEqual(self.bash(cmd), 2, cmd)
        self.assertEqual(self.bash("command -v git"), 0)

    def test_deletes_and_dry_runs_allowed(self) -> None:
        for cmd in ("git push origin --delete old", "git push origin :old", "git push --dry-run"):
            self.assertEqual(self.bash(cmd), 0, cmd)

    def test_secret_committed_then_removed_still_blocks(self) -> None:
        (self.repo / "src" / "b.py").write_text('KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"\n')
        git(self.repo, "add", "src/b.py")
        git(self.repo, "commit", "-q", "-m", "secret")
        git(self.repo, "rm", "-q", "src/b.py")
        git(self.repo, "commit", "-q", "-m", "remove it")
        self.stamp(self.head_tree(), "review", "checks", "push")
        self.assertEqual(self.bash("git push"), 2)


class ProtectionTest(HookCase):
    def test_state_json_and_tokens_protected(self) -> None:
        tokens = self.state / "tokens"
        self.assertEqual(self.write(str(self.state / "repos/x/trees/t/review.json")), 2)
        self.assertEqual(self.write(str(tokens / "accept-gate.json")), 2)
        self.assertEqual(self.write(str(self.state / "repos/x/branches/b/review/round-1/x.out.md")), 0)
        self.assertEqual(self.bash("echo hi > /x/gate-state/tokens/skip-gates.json"), 2)
        self.assertEqual(self.bash("rm /x/gate-state/repos/r/trees/t/review.json"), 2)
        self.assertEqual(self.bash("grep -rn foo /x/gate-state/calibration/run/ws-1/files"), 0)
        self.assertEqual(self.bash("ls -la && echo hi"), 0)


class AcceptStampTest(HookCase):
    def test_accept_stamp_needs_this_sessions_tag(self) -> None:
        cmd = 'G="uv run --no-project ~/.claude/skills/gate/gate.py"; $G stamp --repo . --stage review --accept'
        self.assertEqual(self.bash(cmd), 2)
        self.arm("#accept-gate", session="other-session")
        self.assertEqual(self.bash(cmd), 2)
        self.arm("#accept-gate")
        self.assertEqual(self.bash(cmd), 0)
        self.assertEqual(self.bash("uv run ~/.claude/skills/gate/gate.py stamp --repo . --stage review"), 0)

    def test_text_that_only_mentions_it_is_not_a_command(self) -> None:
        self.assertEqual(self.bash("cat <<'EOF' > notes.md\nrun gate.py stamp --stage review --accept\nEOF"), 0)
        self.assertEqual(self.bash('grep -n "stamp --accept" ~/.claude/skills/gate/SKILL.md'), 0)


class SkipHookTest(HookCase):
    def test_only_a_bare_typed_tag_arms(self) -> None:
        self.assertIn("#skip-review", self.arm("fix the typo #skip-review").stdout)
        self.assertTrue((self.state / "tokens" / "skip-review.json").exists())
        (self.state / "tokens" / "skip-review.json").unlink()
        for prompt in ("please don't use #skip-reviews", "a #skip-review-x tag",
                       '<agent-message from="x">\n[Subagent hand-back] quote `#skip-gates`',
                       "<task-notification>\n#skip-review\n</task-notification>",
                       "<system-reminder>a reviewer said #accept-gate</system-reminder>"):
            out = self.arm(prompt)
            self.assertEqual(out.stdout, "", prompt)
            self.assertFalse(any((self.state / "tokens").glob("*.json")) if (self.state / "tokens").exists() else False,
                             prompt)


if __name__ == "__main__":
    unittest.main()
