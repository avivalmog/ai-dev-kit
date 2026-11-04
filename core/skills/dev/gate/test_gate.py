"""Tests for gate.py: tiers, tighten-only merge, quote checks, stamps, hygiene, summary.

Run: uv run --no-project --python 3.12 python -m unittest discover -s <this folder> -p 'test_*.py'
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("gate", SKILL_DIR / "gate.py")
gate = importlib.util.module_from_spec(_spec)
sys.modules["gate"] = gate
_spec.loader.exec_module(gate)


def git(repo: Path, *args: str) -> str:
    """Run git in a test repo."""
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def write(repo: Path, rel: str, text: str) -> None:
    """Write a file in a test repo."""
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class RepoCase(unittest.TestCase):
    """A temp repo with `main` holding a base commit and a checked-out `feat/x` branch."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "user.email", "t@t")
        write(self.repo, "src/pkg/mod.py", 'def f(x):\n    """Doc."""\n    return x + 1\n')
        write(self.repo, "docs/guide.md", "# Guide\n")
        write(self.repo, "tests/test_mod.py", "from pkg.mod import f\n\ndef test_f():\n    assert f(1) == 2\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "checkout", "-q", "-b", "feat/x")
        self.home = root / "home"
        patches = {"REVIEWERS_DIR": self.home / "reviewers", "SHARED_CALIBRATION": self.home / "calibration",
                   "GATE_STATE": self.home / "state", "TOKENS_DIR": self.home / "tokens",
                   "CALIBRATION_RUNS": self.home / "runs"}
        self.patchers = [mock.patch.object(gate, k, v) for k, v in patches.items()]
        for p in self.patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def cfg(self) -> dict:
        return gate.load_config(self.repo)

    def stage(self, rel: str, text: str) -> None:
        write(self.repo, rel, text)
        git(self.repo, "add", rel)

    def base_commit(self, files: dict[str, str]) -> None:
        """Commit `files` on main and restart feat/x there, so the merge-base already has them."""
        git(self.repo, "checkout", "-q", "main")
        for rel, text in files.items():
            write(self.repo, rel, text)
        git(self.repo, "add", *files)
        git(self.repo, "commit", "-q", "-m", "base files")
        git(self.repo, "checkout", "-q", "-B", "feat/x")

    def tier(self) -> str:
        cfg = self.cfg()
        t = gate.staged_target(self.repo, cfg)
        return gate.compute_tier(cfg, t.files, t.lines, lambda f: gate.ast_neutral(self.repo, t.start, t.end, f))[0]


class GlobTest(unittest.TestCase):
    def test_semantics(self) -> None:
        m = gate.first_match
        self.assertTrue(m("a.py", ["**/*.py"]))
        self.assertTrue(m("x/y/a.py", ["**/*.py"]))
        self.assertFalse(m("x/a.py", ["*.py"]))
        self.assertTrue(m("docs/a/b.md", ["docs/**"]))
        self.assertTrue(m("HANDOFF.md", ["HANDOFF.md"]))
        self.assertFalse(m("sub/HANDOFF.md", ["HANDOFF.md"]))
        self.assertTrue(m("out.jsonl", ["/*.jsonl"]))
        self.assertFalse(m("data/out.jsonl", ["/*.jsonl"]))
        self.assertTrue(m("a/scratch1/x.py", ["**/scratch*/**"]))

    def test_plan_table_paths(self) -> None:
        plan = ("| file | role |\n| --- | --- |\n| `reviewers/gates.toml` | x |\n"
                "| `reviewers/calibration/<id>/…` | y |\n| `~/.claude/x.md` | z |\n"
                "| `docs/a.yaml`, `docs/B.md` | w |\n| `data/styles/Context & Persona/new.yaml` | v |\n")
        paths = gate.plan_table_paths(plan)
        self.assertEqual(paths, ["reviewers/gates.toml", "reviewers/calibration/*/**", "docs/a.yaml", "docs/B.md",
                                 "data/styles/Context & Persona/new.yaml"])
        self.assertTrue(gate.first_match("reviewers/calibration/inv/caught/x.py", paths))


class MergeTest(unittest.TestCase):
    def setUp(self) -> None:
        import tomllib
        self.default = tomllib.loads(gate.DEFAULT_GATES.read_text())

    def test_tighten_ok(self) -> None:
        cfg = gate.merge_config(self.default, {"tiers": {"lite": {"max_changed_lines": 10, "paths": ["docs/**"]},
                                                         "full": {"always_when": ["src/risky/**"]}},
                                               "commit": {"junk_patterns": ["outputs/**"]},
                                               "globs": {"data": ["src/services/**"]}})
        self.assertEqual(cfg["tiers"]["lite"]["max_changed_lines"], 10)
        self.assertIn("src/risky/**", cfg["tiers"]["full"]["always_when"])
        self.assertIn("**/*.log", cfg["commit"]["junk_patterns"])
        self.assertEqual(cfg["globs"]["data"][-1], "src/services/**")
        self.assertIn("**/migrations/**", cfg["globs"]["data"])

    def test_loosening_refused(self) -> None:
        for bad in ({"tiers": {"lite": {"max_changed_lines": 999}}},
                    {"tiers": {"lite": {"paths": ["src/**"]}}},
                    {"commit": {"max_file_kb": 99999}},
                    {"shared": [{"id": "x"}]},
                    {"nonsense": 1},
                    {"globs": {"code": ["*.c"]}}):
            with self.assertRaises(gate.GateError, msg=str(bad)):
                gate.merge_config(self.default, bad)

    def test_project_checks_replace_fallback(self) -> None:
        cfg = gate.merge_config(self.default, {"checks": [{"id": "lint", "kind": "mechanical", "when": ["**"], "run": "true"}]})
        self.assertEqual([c.id for c in gate.all_checks(cfg)], ["lint"])


class TierTest(RepoCase):
    def test_docs_only_is_lite(self) -> None:
        self.stage("docs/guide.md", "# Guide\n\nMore.\n")
        self.assertEqual(self.tier(), "lite")

    def test_docstring_and_hints_only_is_lite(self) -> None:
        self.stage("src/pkg/mod.py", 'def f(x: int) -> int:\n    """Better doc."""\n    return x + 1\n')
        self.assertEqual(self.tier(), "lite")

    def test_new_model_field_is_not_lite(self) -> None:
        self.base_commit({"src/pkg/model.py": "class Row:\n    probe: str\n"})
        self.stage("src/pkg/model.py", "class Row:\n    probe: str\n    severity: str\n")
        self.assertEqual(self.tier(), "standard")

    def test_framework_read_annotations_are_behaviour(self) -> None:
        typer_cmd = 'import typer\n\ndef run(n: bool = typer.Option(False, "--n")):\n    return n\n'
        self.base_commit({"src/pkg/cli.py": typer_cmd})
        self.stage("src/pkg/cli.py", typer_cmd.replace("n: bool", "n: int"))
        self.assertEqual(self.tier(), "standard")                  # typer parses the option from the annotation
        decorated = '@app.get("/x")\ndef h() -> Item:\n    return 1\n'
        self.assertNotEqual(gate.behaviour_dump(decorated), gate.behaviour_dump(decorated.replace("Item", "Other")))
        plain = "def g(x: int) -> int:\n    return x\n"
        self.assertEqual(gate.behaviour_dump(plain), gate.behaviour_dump(plain.replace("int", "str")))

    def test_string_literal_edit_is_standard(self) -> None:
        self.stage("src/pkg/mod.py", 'def f(x):\n    """Doc."""\n    return str(x) + "!"\n')
        self.assertEqual(self.tier(), "standard")

    def test_large_change_is_full(self) -> None:
        self.stage("src/pkg/big.py", "".join(f"V{i} = {i}\n" for i in range(400)))
        self.assertEqual(self.tier(), "full")

    def test_risky_file_is_full(self) -> None:
        self.stage("reviewers/gates.toml", 'version = 1\n[tiers.full]\nalways_when = ["src/pkg/mod.py"]\n')
        self.stage("src/pkg/mod.py", 'def f(x):\n    return x + 2\n')
        self.assertEqual(self.tier(), "full")

    def test_unstaged_edit_on_staged_path_refused(self) -> None:
        self.stage("src/pkg/mod.py", "X = 1\n")
        write(self.repo, "src/pkg/mod.py", "X = 2\n")
        with self.assertRaises(gate.GateError):
            gate.staged_target(self.repo, self.cfg())


class SelectionTest(RepoCase):
    def test_reasons_and_staleness(self) -> None:
        self.stage("docs/guide.md", "# Guide v2\n")
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        cfg = self.cfg()
        t = gate.staged_target(self.repo, cfg)
        picks = {p.id: p for _, p in gate.pick_reviewers(cfg, gate.all_reviewers(cfg, self.repo), t.files, "standard",
                                                          gate.REVIEW_STAGES)}
        self.assertTrue(picks["polish"].selected)
        self.assertEqual(picks["polish"].files, ["src/pkg/mod.py"])
        self.assertIn("matched", picks["docs-drift"].reason)
        tree1 = t.end
        self.stage("docs/guide.md", "# Guide v3\n")
        tree2 = gate.index_tree(self.repo)
        self.assertTrue(gate.still_clean(self.repo, tree1, tree2, ["**/*.py"]))
        self.assertFalse(gate.still_clean(self.repo, tree1, tree2, ["**"]))

    def test_fixtures_hidden_from_reviewers(self) -> None:
        self.stage("reviewers/calibration/x/caught/files/bad.py", "TIMEOUT = 37\n")
        cfg = self.cfg()
        t = gate.staged_target(self.repo, cfg)
        _, _, picks = gate.review_selection(self.repo, cfg, t, gate.argparse.Namespace(only=None, tripwire=False))
        self.assertFalse(any(p.selected for _, p in picks))
        with self.assertRaises(gate.GateError):
            gate.merge_config(gate.tomllib.loads(gate.DEFAULT_GATES.read_text()), {"exclude_paths": ["src/**"]})

    def test_only_uses_its_own_dir_and_the_configured_reviewer(self) -> None:
        write(self.repo, "reviewers/gates.toml", 'version = 1\n[[reviewer]]\nid = "guide-rules"\nstage = "logic"\n'
              'kind = "rules"\nrules = ["docs/guide.md"]\napplies_to = ["docs/**"]\n')
        self.stage("docs/guide.md", "# Guide\n\nMore.\n")
        parse = gate.build_parser().parse_args
        only = parse(["review-prepare", "--repo", str(self.repo), "--only", "rules:docs/guide.md"])
        self.assertEqual(gate.review_dir(self.repo, only).name, gate.ADHOC_DIR)
        self.assertEqual(gate.review_dir(self.repo, parse(["review-ingest", "--repo", str(self.repo)])).name, "review")
        cfg = self.cfg()
        _, _, picks = gate.review_selection(self.repo, cfg, gate.staged_target(self.repo, cfg), only)
        self.assertEqual([r.id for r, _ in picks], ["guide-rules"])
        other = parse(["review-prepare", "--repo", str(self.repo), "--only", "rules:tests/test_mod.py"])
        _, _, picks = gate.review_selection(self.repo, cfg, gate.staged_target(self.repo, cfg), other)
        self.assertEqual(picks[0][0].origin, "adhoc")

    def test_still_clean_ignores_excluded_paths(self) -> None:
        clean_tree = gate.index_tree(self.repo)
        self.stage("reviewers/calibration/lean/caught/files/a.py", "X = 37\n")
        now = gate.index_tree(self.repo)
        self.assertTrue(gate.still_clean(self.repo, clean_tree, now, ["**"], ["reviewers/calibration/**"]))
        self.assertFalse(gate.still_clean(self.repo, clean_tree, now, ["**"]))

    def test_only_rounds_never_count_against_the_review(self) -> None:
        self.stage("docs/guide.md", "# Guide\n\nMore.\n")
        tree = gate.index_tree(self.repo)
        _, rdir = gate.next_round(gate.branch_dir(self.repo) / gate.ADHOC_DIR)
        (rdir / "x.out.md").write_text(
            '```json\n{"reviewer": "rules-guide", "tree": "%s", "checked": ["docs/guide.md"], "findings": [{"id": "R1", '
            '"severity": "blocker", "file": "docs/guide.md", "line": 3, "quote": "More."}]}\n```' % tree)
        gate.save_json(rdir / "dispatch.json", {"tree": tree, "dispatch": [
            {"id": "rules-guide", "out": str(rdir / "x.out.md"), "files": ["docs/guide.md"]}]})
        args = gate.build_parser().parse_args(["review-ingest", "--repo", str(self.repo), "--only", "rules:docs/guide.md"])
        self.assertEqual(gate.cmd_review_ingest(args), gate.EXIT_FAIL)
        self.assertNotIn("review", gate.load_json(gate.branch_dir(self.repo) / "attempts.json", {}))

    def test_targeted_tests(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        cfg = self.cfg()
        self.assertEqual(gate.targeted_tests(self.repo, cfg, ["src/pkg/mod.py"]), ["tests/test_mod.py"])


class OutputTest(RepoCase):
    def output(self, tree: str, findings: list, checked: list) -> str:
        return "notes\n```json\n" + json.dumps({"reviewer": "r", "tree": tree, "checked": checked,
                                                 "findings": findings}) + "\n```\n"

    def test_quote_check_and_coverage(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    timeout = 37\n    return x\n")
        tree = gate.index_tree(self.repo)
        real = {"id": "MV1", "severity": "should-fix", "file": "src/pkg/mod.py", "line": 2, "quote": "timeout = 37"}
        fake = {"id": "MV1", "severity": "blocker", "file": "src/pkg/mod.py", "line": 2, "quote": "retries = 9"}
        judged = gate.judge_output(self.output(tree, [real, fake], ["src/pkg/mod.py"]), "r", ["src/pkg/mod.py"],
                                   self.repo, tree, self.repo, {}, tree)
        self.assertEqual([f["state"] for f in judged["findings"]], ["open", "dropped"])
        self.assertEqual(judged["status"], "findings")
        missing = gate.judge_output(self.output(tree, [], []), "r", ["src/pkg/mod.py"], self.repo, tree, self.repo, {}, tree)
        self.assertEqual(missing["status"], "invalid")
        wrong_tree = gate.judge_output(self.output("abc", [], ["src/pkg/mod.py"]), "r", ["src/pkg/mod.py"],
                                       self.repo, tree, self.repo, {}, tree)
        self.assertEqual(wrong_tree["status"], "invalid")

    def test_score_fixture(self) -> None:
        fx = Path(self.tmp.name) / "fx"
        write(fx, "files/a.py", "X = 1\nTIMEOUT = 37\n")
        write(fx, "expected.json", json.dumps({"id": "MV1", "file": "a.py", "line": 2}))
        hit = {"id": "MV1", "severity": "should-fix", "file": "a.py", "line": 2, "quote": "TIMEOUT = 37"}
        self.assertEqual(gate.score_fixture(self.output("t", [hit], ["a.py"]), fx, "r")["result"], "caught")
        self.assertEqual(gate.score_fixture(self.output("t", [], ["a.py"]), fx, "r")["result"], "missed")
        write(fx, "expected.json", json.dumps({"clean": True}))
        self.assertEqual(gate.score_fixture(self.output("t", [hit], ["a.py"]), fx, "r")["result"], "noisy")
        nit = {**hit, "severity": "nit"}
        self.assertEqual(gate.score_fixture(self.output("t", [nit], ["a.py"]), fx, "r")["result"], "clean")
        write(fx, "expected.json", json.dumps({"id": "MV1", "file": "a.py", "line": 2}))
        self.assertEqual(gate.score_fixture(self.output("t", [nit], ["a.py"]), fx, "r")["result"], "caught")


class StampTest(RepoCase):
    def test_attempt_cap_counts_failures_only(self) -> None:
        cfg = self.cfg()
        for _ in range(cfg["caps"]["review_rounds"] - 1):
            gate.count_failure(self.repo, "review")
        gate.check_cap(self.repo, "review", cfg)          # passing runs cost nothing; one failure left
        gate.count_failure(self.repo, "review")
        with self.assertRaises(gate.GateError) as ctx:
            gate.check_cap(self.repo, "review", cfg)
        self.assertEqual(ctx.exception.code, gate.EXIT_CAP)

    def test_missed_canary_leaves_round_unclean(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        tree = gate.index_tree(self.repo)
        stage_dir = gate.branch_dir(self.repo) / "review"
        _, rdir = gate.next_round(stage_dir)
        clean = '```json\n{"reviewer": "lean", "tree": "%s", "checked": ["src/pkg/mod.py"], "findings": []}\n```' % tree
        (rdir / "lean.out.md").write_text(clean)
        fx = Path(self.tmp.name) / "fx"
        write(fx, "files/a.py", "TIMEOUT = 37\n")
        write(fx, "expected.json", json.dumps({"id": "MV1", "file": "a.py", "line": 1}))
        (rdir / "canary.out.md").write_text('```json\n{"reviewer": "x", "tree": "t", "checked": ["a.py"], "findings": []}\n```')
        gate.save_json(rdir / "dispatch.json", {"tree": tree, "dispatch": [
            {"id": "lean", "out": str(rdir / "lean.out.md"), "files": ["src/pkg/mod.py"]}],
            "canary": {"id": "magic-values", "out": str(rdir / "canary.out.md"), "fixture": str(fx)}})
        report = gate.ingest_round(self.repo, rdir, stage_dir, self.repo, tree)
        self.assertEqual(report["canary"]["result"], "missed")
        self.assertIsNone(gate.load_json(stage_dir / "state.json", {})["lean"]["clean_tree"])

    def test_review_stamp_needs_clean_reviewers_or_token(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        cfg = self.cfg()
        with self.assertRaises(gate.GateError):
            gate.stamp_review(self.repo, cfg, accept=False)
        with self.assertRaises(gate.GateError):
            gate.stamp_review(self.repo, cfg, accept=True)       # no token armed
        gate.TOKENS_DIR.mkdir(parents=True)
        (gate.TOKENS_DIR / "accept-gate.json").write_text(json.dumps({"created": time.time(), "session": "s"}))
        self.assertEqual(gate.stamp_review(self.repo, cfg, accept=True)["verdict"], "ACCEPTED")
        self.assertFalse((gate.TOKENS_DIR / "accept-gate.json").exists())

    def test_restamp_keeps_the_branch_rounds_and_nits(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        cfg, tree = self.cfg(), gate.index_tree(self.repo)
        stage_dir = gate.branch_dir(self.repo) / "review"
        _, rdir = gate.next_round(stage_dir)
        (rdir / "lean.out.md").write_text(
            '```json\n{"reviewer": "lean", "tree": "%s", "checked": ["src/pkg/mod.py"], "findings": [{"id": "LN4", '
            '"severity": "nit", "file": "src/pkg/mod.py", "line": 2, "quote": "return x * 3"}]}\n```' % tree)
        gate.save_json(rdir / "dispatch.json", {"tree": tree, "dispatch": [
            {"id": "lean", "out": str(rdir / "lean.out.md"), "files": ["src/pkg/mod.py"]}]})
        gate.ingest_round(self.repo, rdir, stage_dir, self.repo, tree)
        _, _, picks = gate.review_selection(self.repo, cfg, gate.staged_target(self.repo, cfg),
                                            gate.argparse.Namespace(only=None, tripwire=False))
        gate.save_json(stage_dir / "state.json", {r.id: {"clean_tree": tree} for r, p in picks if p.selected})
        gate.stamp_review(self.repo, cfg, accept=False)
        again = gate.stamp_review(self.repo, cfg, accept=False)      # nothing re-ran: still the branch's story
        self.assertEqual([x["round"] for x in again["rounds"]], ["round-1"])
        self.assertEqual([f["id"] for f in again["open_nits"]], ["LN4"])

    def test_ingest_refuses_missing_output(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        stage_dir = gate.branch_dir(self.repo) / "review"
        _, rdir = gate.next_round(stage_dir)
        tree = gate.index_tree(self.repo)
        gate.save_json(rdir / "dispatch.json", {"tree": tree, "dispatch": [
            {"id": "lean", "out": str(rdir / "lean.out.md"), "files": ["src/pkg/mod.py"]}]})
        report = gate.ingest_round(self.repo, rdir, stage_dir, self.repo, tree)
        self.assertEqual(report["results"][0]["status"], "invalid")
        self.assertIsNone(gate.load_json(stage_dir / "state.json", {})["lean"]["clean_tree"])


class DeltaReviewTest(RepoCase):
    """After a reviewer's first valid review it sees only what changed since, plus its own open findings."""

    BIG = "".join(f"V{i} = {i}\n" for i in range(1, 21))

    def setUp(self) -> None:
        super().setUp()
        self.base_commit({"src/pkg/big.py": self.BIG, "src/pkg/other.py": "W = 1\n"})
        gate.SHARED_CALIBRATION.mkdir(parents=True)
        gate.REVIEWERS_DIR.mkdir(parents=True)
        record = {}
        for r in gate.all_reviewers(self.cfg(), self.repo):
            if r.origin == "shared":
                r.source[0].write_text(f"---\nname: review-{r.id}\n---\nbody\n")
                record[r.id] = {"sha": gate.calibration_hash(r)}
        gate.save_json(gate.SHARED_CALIBRATION / "calibrated.json", record)
        self.stage_dir = gate.branch_dir(self.repo) / "review"

    def prepare(self) -> dict:
        with contextlib.redirect_stdout(io.StringIO()):
            gate.cmd_review_prepare(gate.build_parser().parse_args(["review-prepare", "--repo", str(self.repo)]))
        return gate.load_json(gate.latest_round(self.stage_dir, None) / "dispatch.json", {})

    def answer(self, dispatch: dict, findings: dict | None = None, invalid: tuple = (), unread: tuple = ()) -> dict:
        """Every dispatched reviewer reports what it was assigned; `findings` maps a reviewer to its findings."""
        for e in dispatch["dispatch"]:
            body = {"reviewer": e["id"], "tree": dispatch["tree"], "checked": [] if e["id"] in unread else e["files"],
                    "findings": (findings or {}).get(e["id"], [])}
            Path(e["out"]).write_text("no block" if e["id"] in invalid else f"```json\n{json.dumps(body)}\n```")
        rdir = Path(dispatch["dispatch"][0]["out"]).parent
        return gate.ingest_round(self.repo, rdir, self.stage_dir, self.repo, dispatch["tree"])

    @staticmethod
    def finding(line: int, severity: str = "blocker", rid: str = "LN1", path: str = "src/pkg/big.py",
                quote: str | None = None) -> dict:
        return {"id": rid, "severity": severity, "file": path, "line": line, "quote": quote or f"V{line} = {line}",
                "why": "w", "fix": "f", "plain": "p"}

    def edit_big(self, line: int) -> None:
        """Change one more line of big.py; earlier edits stay."""
        text = (self.repo / "src/pkg/big.py").read_text()
        self.stage("src/pkg/big.py", text.replace(f"V{line} = {line}\n", f"V{line} = {line * 100}\n"))

    def result(self, report: dict, rid: str) -> dict:
        return next(r for r in report["results"] if r["id"] == rid)

    def reject(self, finding: str, *extra: str) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            gate.cmd_reject(gate.build_parser().parse_args(["reject", "--repo", str(self.repo), "--finding", finding,
                                                            "--cite", "src/pkg/big.py:2", "--reason", "wrong", *extra]))

    def test_a_reviewer_that_skipped_its_files_runs_and_is_stamped_one_model_tier_up(self) -> None:
        self.edit_big(5)
        first = self.prepare()
        self.assertEqual(next(e for e in first["dispatch"] if e["id"] == "polish")["model_tier"], "standard")
        self.assertEqual(self.result(self.answer(first, unread=("polish",)), "polish")["status"], "invalid")
        again = self.prepare()
        self.assertEqual(next(e for e in again["dispatch"] if e["id"] == "polish")["model_tier"], "strong")
        self.answer(again)
        stamp = gate.stamp_review(self.repo, self.cfg(), accept=False)
        self.assertEqual(next(s for s in stamp["selected"] if s["id"] == "polish")["model"], "strong")

    def test_a_second_round_sees_only_the_change_and_rechecks_open_findings(self) -> None:
        self.edit_big(5)
        first = self.prepare()
        self.assertTrue(all("base" not in e for e in first["dispatch"]))            # round 1: the full change
        self.answer(first, {"correctness": [self.finding(2)]})
        self.stage("src/pkg/other.py", "W = 2\n")
        second = self.prepare()
        correctness = next(e for e in second["dispatch"] if e["id"] == "correctness")
        self.assertEqual((correctness["base"], correctness["files"]), (first["tree"], ["src/pkg/big.py", "src/pkg/other.py"]))
        self.assertEqual([p["line"] for p in correctness["prior"]], [2])
        prompt = Path(correctness["prompt"]).read_text()
        self.assertIn("This is a re-review", prompt)
        self.assertIn("read only the changed parts", prompt)
        self.assertIn(first["tree"], prompt)
        self.assertIn("quote: `V2 = 2`", prompt)
        patch = Path(prompt.split("The diff under review: `", 1)[1].split("`", 1)[0]).read_text()
        self.assertIn("+W = 2", patch)
        self.assertNotIn("V5", patch)                                                # reviewed already
        fidelity = next(e for e in second["dispatch"] if e["id"] == "fidelity")
        self.assertEqual(fidelity["files"], ["src/pkg/other.py"])                    # clean, so only what changed

    def test_a_test_only_change_reruns_only_the_tests_reviewer(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare())
        self.stage("tests/test_mod.py", "def test_f():\n    assert True\n")
        self.assertEqual([e["id"] for e in self.prepare()["dispatch"]], ["tests"])

    def test_a_new_should_fix_off_the_changed_lines_is_kept_as_a_nit(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [self.finding(8)]})
        self.edit_big(12)
        report = self.answer(self.prepare(), {"correctness": [
            self.finding(3, severity="should-fix"), self.finding(12, severity="should-fix", quote="V12 = 1200"), self.finding(18)]})
        correctness = self.result(report, "correctness")
        self.assertEqual([f["severity"] for f in correctness["findings"]], ["nit", "should-fix", "blocker"])
        self.assertIn("unchanged since", correctness["findings"][0]["late"])
        self.assertEqual(correctness["status"], "findings")
        self.edit_big(14)
        report = self.answer(self.prepare(), {"correctness": [self.finding(3, severity="should-fix")]})
        self.assertEqual(self.result(report, "correctness")["status"], "clean")             # only a late one: passes

    def test_a_real_defect_blocks_and_a_nit_does_not(self) -> None:
        self.edit_big(5)
        report = self.answer(self.prepare(), {"correctness": [self.finding(3, severity="should-fix")],
                                              "polish": [self.finding(4, severity="nit", rid="MV1")]})
        self.assertEqual(self.result(report, "correctness")["status"], "findings")
        self.assertEqual(self.result(report, "polish")["status"], "clean")

    def test_an_older_bug_costs_no_attempt_and_waits_for_the_user(self) -> None:
        self.edit_big(5)
        older = self.finding(3, severity="should-fix") | {"older": True}
        report = self.answer(self.prepare(), {"correctness": [older]})
        self.assertEqual(self.result(report, "correctness")["status"], "clean")
        with self.assertRaises(gate.GateError) as ctx:
            gate.stamp_review(self.repo, self.cfg(), accept=False)
        self.assertIn("older bug", str(ctx.exception))
        gate.TOKENS_DIR.mkdir(parents=True)
        (gate.TOKENS_DIR / "accept-gate.json").write_text(json.dumps({"created": time.time(), "session": "s"}))
        accepted = gate.stamp_review(self.repo, self.cfg(), accept=True)
        self.assertEqual((accepted["verdict"], accepted["rounds"][-1]["blocking_found"]), ("ACCEPTED", 0))
        stamp = gate.stamp_review(self.repo, self.cfg(), accept=False)                       # not asked twice
        self.assertEqual((stamp["verdict"], [b["id"] for b in stamp["open_blocking"]]), ("PASS", ["LN1"]))  # on record

    def test_an_older_bug_fixed_with_its_text_still_there_clears_at_the_re_check(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [self.finding(3, severity="should-fix") | {"older": True}]})
        text = (self.repo / "src/pkg/big.py").read_text()
        self.stage("src/pkg/big.py", text.replace("V3 = 3\n", "if not READY:\n    raise ValueError\nV3 = 3\n"))
        second = self.prepare()
        correctness = next(e for e in second["dispatch"] if e["id"] == "correctness")
        self.assertEqual([p["line"] for p in correctness["prior"]], [3])                     # it is asked to re-check
        self.answer(second)
        self.assertEqual(gate.stamp_review(self.repo, self.cfg(), accept=False)["verdict"], "PASS")

    def test_an_older_bug_whose_text_left_the_code_clears(self) -> None:
        import gate_view

        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [self.finding(3, severity="should-fix") | {"older": True}]})
        text = (self.repo / "src/pkg/big.py").read_text()
        self.stage("src/pkg/big.py", text.replace("V3 = 3\n", "FIXED = 3\n"))
        self.assertEqual(gate_view.summary(gate.branch_dir(self.repo), {}, None, self.repo)["older"], [])

    def test_a_new_older_should_fix_on_unchanged_lines_is_only_listed_on_a_re_review(self) -> None:
        import gate_view

        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [self.finding(8)]})
        self.edit_big(12)
        report = self.answer(self.prepare(), {"correctness": [self.finding(3, severity="should-fix") | {"older": True}]})
        self.assertEqual(self.result(report, "correctness")["findings"][0]["severity"], "nit")
        self.assertEqual(gate_view.summary(gate.branch_dir(self.repo), {}, None, self.repo)["older"], [])

    def test_a_new_older_blocker_on_a_re_review_still_asks_the_user(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [self.finding(8)]})
        self.edit_big(12)
        report = self.answer(self.prepare(), {"correctness": [self.finding(3) | {"older": True}]})
        self.assertEqual(self.result(report, "correctness")["findings"][0]["severity"], "blocker")
        with self.assertRaises(gate.GateError) as ctx:
            gate.stamp_review(self.repo, self.cfg(), accept=False)
        self.assertIn("older bug", str(ctx.exception))

    def test_an_older_bug_from_the_first_review_stays_the_users_call(self) -> None:
        older = self.finding(3, severity="should-fix") | {"older": True}
        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [older]})
        self.edit_big(12)
        report = self.answer(self.prepare(), {"correctness": [older]})
        self.assertEqual(self.result(report, "correctness")["findings"][0]["severity"], "should-fix")
        with self.assertRaises(gate.GateError) as ctx:
            gate.stamp_review(self.repo, self.cfg(), accept=False)
        self.assertIn("older bug", str(ctx.exception))

    def test_a_re_review_runs_at_standard_after_a_first_review_at_strong(self) -> None:
        def correctness(dispatch: dict) -> dict:
            return next(e for e in dispatch["dispatch"] if e["id"] == "correctness")

        self.stage("src/pkg/other.py", "W = 1\n" + "# pad\n" * 400)                   # a big change: the full tier
        first = self.prepare()
        self.assertEqual(correctness(first)["model_tier"], "strong")
        self.answer(first)
        self.edit_big(5)
        second = self.prepare()
        self.assertEqual((correctness(second)["model_tier"], correctness(second)["base"]), ("standard", first["tree"]))
        self.assertIn("do not look for other problems in unchanged code", Path(correctness(second)["prompt"]).read_text())
        self.answer(second, unread=("correctness",))
        self.assertEqual(correctness(self.prepare())["model_tier"], "strong")       # it did not read its files: one up

    def test_a_finding_marked_older_still_blocks_outside_the_code_review(self) -> None:
        self.edit_big(5)
        tree = gate.index_tree(self.repo)
        for name in (gate.STAGE_PLAN, gate.TRIPWIRE_DIR, gate.ADHOC_DIR):
            with self.subTest(name):
                stage_dir = gate.branch_dir(self.repo) / name
                _, rdir = gate.next_round(stage_dir)
                out = rdir / "lens.out.md"
                body = {"reviewer": "lens", "tree": tree, "checked": [], "findings": [self.finding(2) | {"older": True}]}
                out.write_text(f"```json\n{json.dumps(body)}\n```")
                gate.save_json(rdir / "dispatch.json", {"tree": tree, "dispatch": [{"id": "lens", "out": str(out), "files": []}]})
                report = gate.ingest_round(self.repo, rdir, stage_dir, self.repo, tree)
                self.assertEqual(self.result(report, "lens")["status"], "findings")

    def test_any_new_change_gets_every_reviewer(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare())
        self.stage("src/pkg/other.py", "W = 1\nX = 2\n")
        with self.assertRaises(gate.GateError):
            gate.stamp_review(self.repo, self.cfg(), accept=False)
        self.assertEqual(sorted(e["id"] for e in self.prepare()["dispatch"]),
                         ["correctness", "docs-drift", "fidelity", "polish", "tests"])

    def test_the_reviewer_of_an_older_bug_re_checks_a_test_only_fix(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [self.finding(3, severity="should-fix") | {"older": True}]})
        self.stage("tests/test_mod.py", "def test_f():\n    assert True\n")
        self.assertEqual(sorted(e["id"] for e in self.prepare()["dispatch"]), ["correctness", "tests"])

    def test_rejecting_a_finding_keeps_the_older_bug_re_check(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [self.finding(2), self.finding(3, severity="should-fix") | {"older": True}]})
        self.reject("correctness#0")
        self.assertTrue(gate.load_json(self.stage_dir / "state.json", {})["correctness"]["older"])

    def test_a_rejected_older_bug_stops_asking_and_forces_no_re_check(self) -> None:
        import gate_view

        older = self.finding(3, severity="should-fix") | {"older": True}
        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [older]})
        self.edit_big(6)
        self.answer(self.prepare(), {"correctness": [older]})                        # reported twice, then set aside
        self.reject("correctness#0")
        view = gate_view.summary(gate.branch_dir(self.repo), {}, None, self.repo)
        self.assertEqual(view["older"], [])
        self.assertNotIn("fixed since, or you chose to leave it", gate_view.render(view))
        self.stage("tests/test_mod.py", "def test_f():\n    assert True\n")
        self.assertEqual([e["id"] for e in self.prepare()["dispatch"]], ["tests"])

    def _page_after_two_reports(self, finding: dict) -> str:
        import gate_view

        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [finding]})
        self.edit_big(6)
        self.answer(self.prepare(), {"correctness": [finding]})
        return gate_view.render(gate_view.summary(gate.branch_dir(self.repo), {}, None, self.repo))

    def test_a_finding_reported_again_is_never_shown_as_fixed(self) -> None:
        page = self._page_after_two_reports(self.finding(3))
        self.assertNotIn("Fixed before the next round", page)
        self.assertIn("3 &middot; Found, not handled yet <span class=muted>(1)</span>", page)

    def test_an_older_bug_reported_again_gets_one_card(self) -> None:
        page = self._page_after_two_reports(self.finding(3, severity="should-fix") | {"older": True})
        self.assertIn("3 &middot; Found, not handled yet <span class=muted>(1)</span>", page)

    def test_rejecting_in_an_earlier_round_leaves_a_newer_finding_blocking(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [self.finding(2)]})
        self.stage("tests/test_mod.py", "def test_f():\n    assert True\n")
        self.answer(self.prepare(), {"correctness": [self.finding(3)]})
        self.reject("correctness#0", "--round", "1")
        with self.assertRaises(gate.GateError) as ctx:
            gate.stamp_review(self.repo, self.cfg(), accept=False)
        self.assertIn("correctness: not clean", str(ctx.exception))

    def test_rejecting_in_an_earlier_round_leaves_the_newer_re_check(self) -> None:
        older = self.finding(3, severity="should-fix") | {"older": True}
        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [older]})
        self.edit_big(6)
        self.answer(self.prepare(), {"correctness": [older]})
        self.reject("correctness#0", "--round", "1")
        self.assertTrue(gate.load_json(self.stage_dir / "state.json", {})["correctness"]["older"])

    def test_the_re_review_note_says_which_findings_are_older(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [self.finding(3, severity="should-fix") | {"older": True}]})
        self.edit_big(6)
        correctness = next(e for e in self.prepare()["dispatch"] if e["id"] == "correctness")
        self.assertIn("quote: `V3 = 3` (older: true)", Path(correctness["prompt"]).read_text())

    def test_an_older_nit_forces_no_re_check(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare(), {"polish": [self.finding(3, severity="nit", rid="MV1") | {"older": True}]})
        self.stage("tests/test_mod.py", "def test_f():\n    assert True\n")
        self.assertEqual([e["id"] for e in self.prepare()["dispatch"]], ["tests"])

    def test_only_a_real_true_marks_a_finding_older(self) -> None:
        self.edit_big(5)
        report = self.answer(self.prepare(), {"correctness": [self.finding(3) | {"older": "false"}]})
        self.assertEqual(self.result(report, "correctness")["status"], "findings")

    def test_an_invalid_round_leaves_an_older_bug_open(self) -> None:
        import gate_view

        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [self.finding(3, severity="should-fix") | {"older": True}]})
        self.edit_big(6)
        self.answer(self.prepare(), invalid=("correctness",))
        older = gate_view.summary(gate.branch_dir(self.repo), {}, None, self.repo)["older"]
        self.assertEqual([f["id"] for f in older], ["LN1"])

    def test_a_re_raised_older_bug_is_still_re_checked(self) -> None:
        finding = self.finding(3, severity="should-fix") | {"older": True, "state": "reraised"}
        gate.save_json(self.stage_dir / "round-1" / "ingest.json", {
            "tree": "t", "canary": {"result": "unavailable"},
            "results": [{"id": "correctness", "status": "clean", "error": "", "findings": [finding]}]})
        _, prior = gate.review_base(self.stage_dir, {"correctness": {"round": "round-1"}}, "correctness")
        self.assertEqual([p["line"] for p in prior], [3])

    def test_only_the_reviewer_that_failed_uses_an_attempt(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [self.finding(2)]})
        with contextlib.redirect_stdout(io.StringIO()):
            gate.cmd_review_ingest(gate.build_parser().parse_args(["review-ingest", "--repo", str(self.repo)]))
        path = gate.branch_dir(self.repo) / "attempts.json"
        self.assertEqual(gate.load_json(path, {})[gate.REVIEWER_ATTEMPTS], {"correctness": 1})
        gate.count_failure(self.repo, "review", ["tests"])
        attempts = gate.load_json(path, {})
        self.assertEqual((attempts["review"], attempts[gate.REVIEWER_ATTEMPTS]), (1, {"correctness": 1, "tests": 1}))
        gate.count_failure(self.repo, "review", ["tests"])
        self.assertEqual(gate.load_json(path, {})["review"], 2)                              # the worst reviewer's count
        gate.reset_attempt(self.repo, "review")
        self.assertEqual(gate.load_json(path, {})["review"], 0)
        self.assertNotIn(gate.REVIEWER_ATTEMPTS, gate.load_json(path, {}))

    def test_an_unfixed_earlier_finding_still_blocks(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [self.finding(2, severity="should-fix")]})
        self.stage("src/pkg/other.py", "W = 2\n")
        report = self.answer(self.prepare(), {"correctness": [self.finding(2, severity="should-fix")]})
        correctness = self.result(report, "correctness")
        self.assertEqual((correctness["status"], correctness["findings"][0]["severity"]), ("findings", "should-fix"))

    def test_an_invalid_report_keeps_the_last_valid_base(self) -> None:
        self.edit_big(5)
        first = self.prepare()
        self.answer(first, {"correctness": [self.finding(2)]})
        self.edit_big(12)
        self.answer(self.prepare(), invalid=("correctness",))
        self.edit_big(14)
        correctness = next(e for e in self.prepare()["dispatch"] if e["id"] == "correctness")
        self.assertEqual(correctness["base"], first["tree"])


    def test_every_reviewer_runs_in_one_round(self) -> None:
        self.edit_big(5)
        first = self.prepare()
        self.assertEqual(sorted(e["id"] for e in first["dispatch"]), ["correctness", "docs-drift", "fidelity", "polish", "tests"])
        self.assertNotIn("canary", first)                                            # proven by calibrate only
        self.answer(first, {"correctness": [self.finding(5, quote="V5 = 500")]})
        self.edit_big(6)
        self.assertEqual(sorted(e["id"] for e in self.prepare()["dispatch"]),              # a fix: all read what changed
                         ["correctness", "docs-drift", "fidelity", "polish", "tests"])

    def test_excluded_paths_never_reach_the_reviewer(self) -> None:
        self.edit_big(5)
        self.stage("tests/test_mod.py", "def test_f():\n    assert True\n")
        files = {e["id"]: e["files"] for e in self.prepare()["dispatch"]}
        self.assertEqual(files["polish"], ["src/pkg/big.py"])
        self.assertEqual(files["tests"], ["src/pkg/big.py", "tests/test_mod.py"])


class AuditTest(RepoCase):
    def register_plan(self, files: list[str]) -> None:
        plan = Path(self.tmp.name) / "plan.md"
        plan.write_text("| file | role |\n| --- | --- |\n" + "".join(f"| `{f}` | x |\n" for f in files))
        gate.cmd_init(gate.build_parser().parse_args(["init", "--repo", str(self.repo), "--plan", str(plan)]))

    def hits(self) -> list[str]:
        cfg = self.cfg()
        files, _, binary = gate.numstat(self.repo, "--cached", "HEAD")
        hits, _ = gate.hygiene_hits(self.repo, cfg, files, binary, gate.added_lines(self.repo, "--cached", "HEAD"),
                                    lambda f: gate.blob_size(self.repo, f":{f}"))
        return sorted(f"{h['rule'].split()[0]} {h['file']}" for h in hits)

    def test_h1_to_h4(self) -> None:
        self.register_plan(["src/pkg/mod.py", "assets/**"])
        self.stage("src/pkg/mod.py", 'P = "/tmp/claude-1000/x/scratchpad/a.py"\n')
        self.stage("check.py", "print(1)\n")
        self.stage("build.log", "log\n")
        write(self.repo, "blob.bin", "")
        (self.repo / "blob.bin").write_bytes(b"\x00" * (2 * 1024 * 1024))
        git(self.repo, "add", "blob.bin")
        self.assertEqual(self.hits(), ["H1 blob.bin", "H1 build.log", "H1 check.py", "H2 build.log",
                                       "H3 src/pkg/mod.py", "H4 blob.bin"])

    def test_no_plan_skips_h1_only(self) -> None:
        self.stage("check.py", "print(1)\n")
        self.assertEqual(self.hits(), [])

    def test_removing_tracked_junk_or_a_forbidden_file_is_no_hit(self) -> None:
        self.stage("build.log", "log\n")
        self.stage(".env", "A=1\n")
        git(self.repo, "commit", "-q", "-m", "junk")
        base = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "rm", "-q", "--cached", "build.log", ".env")
        self.assertEqual(self.hits(), [])
        git(self.repo, "commit", "-q", "-m", "clean up")
        self.assertEqual(gate.branch_commit_hits(self.repo, self.cfg(), base)[0], [])

    def test_template_of_a_forbidden_name_is_no_hit(self) -> None:
        hits = gate.push_hits(self.repo, self.cfg(), [".env.example", ".env.local"], [])
        self.assertEqual([h["file"] for h in hits], [".env.local"])

    def test_leftovers_and_secrets(self) -> None:
        write(self.repo, "notes.txt", "scratch\n")
        self.stage("src/pkg/mod.py", 'KEY = "sk-abcdefghijklmnopqrstuvwxyz123"\n')
        git(self.repo, "commit", "-q", "-m", "x")
        cfg = self.cfg()
        hits = gate.push_hits(self.repo, cfg, ["src/pkg/mod.py"], gate.added_lines(self.repo, "main", "HEAD"))
        self.assertEqual([h["rule"] for h in hits], ["P2 secret"])
        args = gate.build_parser().parse_args(["audit", "--repo", str(self.repo), "--mode", "branch", "--no-freshness"])
        self.assertEqual(gate.cmd_audit(args), gate.EXIT_FAIL)
        report = gate.load_json(gate.tree_dir(self.repo, gate.head_tree(self.repo)) / "audit-branch.json", {})
        self.assertEqual(report["leftovers"], ["notes.txt"])


class ChecksAndStampsTest(RepoCase):
    def gates(self, checks: str) -> None:
        self.stage("reviewers/gates.toml", "version = 1\n" + checks)

    def run_checks(self) -> int:
        return gate.cmd_checks_run(gate.build_parser().parse_args(["checks-run", "--repo", str(self.repo)]))

    def test_failing_group_stops_later_groups_and_blocks_stamp(self) -> None:
        self.gates('[[checks]]\nid = "unit"\nkind = "unit"\nwhen = ["**"]\nrun = "false"\n'
                   '[[checks]]\nid = "suite"\nkind = "unit"\nwhen = ["**"]\nlast = true\nrun = "true"\n')
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        self.assertEqual(self.run_checks(), gate.EXIT_FAIL)
        state = gate.load_json(gate.branch_dir(self.repo) / "checks" / "state.json", {})
        self.assertEqual(state["unit"]["status"], "fail")
        self.assertNotIn("suite", state)
        with self.assertRaises(gate.GateError):
            gate.stamp_checks(self.repo, self.cfg(), accept=False)

    def test_passing_checks_stamp(self) -> None:
        self.gates('[[checks]]\nid = "unit"\nkind = "unit"\nwhen = ["**"]\nrun = "true"\n')
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        self.assertEqual(self.run_checks(), gate.EXIT_OK)
        self.assertEqual(gate.stamp_checks(self.repo, self.cfg(), accept=False)["verdict"], gate.VERDICT_PASS)

    def test_push_stamp_needs_review_checks_and_clean_audit(self) -> None:
        self.stage("src/pkg/mod.py", "X = 1\n")
        git(self.repo, "commit", "-q", "-m", "x")
        with self.assertRaises(gate.GateError):
            gate.stamp_push(self.repo, self.cfg(), accept=False)
        tdir = gate.tree_dir(self.repo, gate.head_tree(self.repo))
        for stage in ("review", "checks"):
            gate.save_json(tdir / f"{stage}.json", {"verdict": gate.VERDICT_PASS})
        gate.save_json(tdir / "audit-branch.json", {"hits": [{"rule": "P2 secret", "file": "x", "detail": "d"}]})
        with self.assertRaises(gate.GateError):
            gate.stamp_push(self.repo, self.cfg(), accept=False)
        gate.save_json(tdir / "audit-branch.json", {"hits": []})
        self.assertEqual(gate.stamp_push(self.repo, self.cfg(), accept=False)["verdict"], gate.VERDICT_PASS)

    def arm_accept(self) -> None:
        gate.TOKENS_DIR.mkdir(parents=True, exist_ok=True)
        (gate.TOKENS_DIR / "accept-gate.json").write_text(json.dumps({"created": time.time(), "session": "s"}))

    def test_accepted_push_lets_its_listed_hits_through_but_never_a_secret(self) -> None:
        audit = gate.build_parser().parse_args(["audit", "--repo", str(self.repo), "--mode", "branch", "--no-freshness"])
        self.stage("build.log", "log\n")
        git(self.repo, "commit", "-q", "-m", "junk")
        self.assertEqual(gate.cmd_audit(audit), gate.EXIT_FAIL)
        tdir = gate.tree_dir(self.repo, gate.head_tree(self.repo))
        for stage in ("review", "checks"):
            gate.save_json(tdir / f"{stage}.json", {"verdict": gate.VERDICT_PASS})
        self.arm_accept()
        gate.save_json(tdir / "push.json", gate.stamp_push(self.repo, self.cfg(), accept=True))
        self.assertEqual(gate.cmd_audit(audit), gate.EXIT_OK)      # the guard's re-audit honours the accept
        self.stage("src/pkg/mod.py", 'KEY = "sk-abcdefghijklmnopqrstuvwxyz123"\n')
        git(self.repo, "commit", "-q", "-m", "secret")
        self.assertEqual(gate.cmd_audit(audit), gate.EXIT_FAIL)
        tdir = gate.tree_dir(self.repo, gate.head_tree(self.repo))
        for stage in ("review", "checks"):
            gate.save_json(tdir / f"{stage}.json", {"verdict": gate.VERDICT_PASS})
        self.arm_accept()
        with self.assertRaises(gate.GateError):
            gate.stamp_push(self.repo, self.cfg(), accept=True)

    def test_exit_code_only_baseline_still_fails(self) -> None:
        self.gates('[[checks]]\nid = "lint"\nkind = "mechanical"\nwhen = ["**"]\nrun = "false"\n')
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        self.assertEqual(self.run_checks(), gate.EXIT_FAIL)
        args = gate.build_parser().parse_args(["checks-run", "--repo", str(self.repo), "--baseline", "lint"])
        self.assertEqual(gate.cmd_checks_run(args), gate.EXIT_FAIL)
        self.assertEqual(gate.load_json(gate.branch_dir(self.repo) / "checks" / "state.json", {})["lint"]["status"], "fail")

    def test_skip_review_waives_only_review_at_push(self) -> None:
        self.stage("src/pkg/mod.py", "X = 1\n")
        git(self.repo, "commit", "-q", "-m", "x")
        tdir = gate.tree_dir(self.repo, gate.head_tree(self.repo))
        gate.save_json(tdir / "user-skip.json", {"kind": "skip-review"})
        gate.save_json(tdir / "audit-branch.json", {"hits": []})
        with self.assertRaises(gate.GateError):
            gate.stamp_push(self.repo, self.cfg(), accept=False)
        gate.save_json(tdir / "checks.json", {"verdict": gate.VERDICT_PASS})
        self.assertEqual(gate.stamp_push(self.repo, self.cfg(), accept=False)["verdict"], gate.VERDICT_PASS)

    def calibrated_review_round(self) -> dict:
        """Calibrate every shared reviewer on a stub body, run review-prepare, return its dispatch.json."""
        gate.SHARED_CALIBRATION.mkdir(parents=True)
        cfg = self.cfg()
        gate.REVIEWERS_DIR.mkdir(parents=True)
        record = {}
        for r in gate.all_reviewers(cfg, self.repo):
            if r.origin == "shared":
                r.source[0].write_text(f"---\nname: review-{r.id}\n---\nbody of {r.id}\n")
                record[r.id] = {"sha": gate.calibration_hash(r)}
        gate.save_json(gate.SHARED_CALIBRATION / "calibrated.json", record)
        args = gate.build_parser().parse_args(["review-prepare", "--repo", str(self.repo)])
        self.assertEqual(gate.cmd_review_prepare(args), gate.EXIT_OK)
        return gate.load_json(gate.latest_round(gate.branch_dir(self.repo) / "review", None) / "dispatch.json", {})

    def test_deleted_file_is_not_assigned_to_read(self) -> None:
        git(self.repo, "rm", "-q", "docs/guide.md")
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        dispatch = self.calibrated_review_round()
        fidelity = next(e for e in dispatch["dispatch"] if e["id"] == "fidelity")
        self.assertNotIn("docs/guide.md", fidelity["files"])
        self.assertIn("src/pkg/mod.py", fidelity["files"])

    def test_polish_and_docs_drift_are_not_on_the_light_tier(self) -> None:
        # A first review reads every file end to end; on light they did not finish big files, so the review was invalid.
        reviewers = {r.id: r for r in gate.all_reviewers(self.cfg(), self.repo)}
        self.assertEqual((reviewers["polish"].model, reviewers["docs-drift"].model), ("standard", "standard"))

    def test_dispatch_entry_is_agent_neutral(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        entry = next(e for e in self.calibrated_review_round()["dispatch"] if e["id"] == "correctness")
        self.assertEqual((entry["agent_name"], entry["model_tier"]), ("review-correctness", "standard"))
        self.assertNotIn("subagent_type", entry)
        fallback = Path(entry["fallback_prompt"]).read_text(encoding="utf-8")
        self.assertTrue(fallback.startswith("body of correctness\n"))
        correctness = next(r for r in gate.all_reviewers(self.cfg(), self.repo) if r.id == "correctness")
        self.assertEqual(correctness.model_for(gate.TIER_FULL), "strong")

    def test_expired_accept_token_refused(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        gate.TOKENS_DIR.mkdir(parents=True)
        (gate.TOKENS_DIR / "accept-gate.json").write_text(json.dumps({"created": time.time() - gate.TOKEN_TTL_S - 1}))
        with self.assertRaises(gate.GateError):
            gate.stamp_review(self.repo, self.cfg(), accept=True)

    def test_plan_tier_wins_when_higher(self) -> None:
        self.stage("docs/guide.md", "# Guide v2\n")
        cfg = self.cfg()
        t = gate.staged_target(self.repo, cfg)
        self.assertEqual(gate.branch_tier(self.repo, cfg, t)[0], "lite")
        gate.save_json(gate.branch_dir(self.repo) / "run.json", {"plan_tier": "full", "plan_tier_reason": "risky"})
        tier, reason = gate.branch_tier(self.repo, cfg, t)
        self.assertEqual(tier, "full")
        self.assertIn("plan gate predicted", reason)


class FreshnessPlanCalibrationTest(RepoCase):
    def test_conflict_with_moved_base_is_a_stale_hit(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        git(self.repo, "commit", "-q", "-m", "branch")
        git(self.repo, "checkout", "-q", "main")
        write(self.repo, "src/pkg/mod.py", "def f(x):\n    return x * 4\n")
        git(self.repo, "commit", "-qam", "main moved")
        git(self.repo, "checkout", "-q", "feat/x")
        hits = gate.freshness_hits(self.repo, self.cfg(), ["src/pkg/mod.py"], fetch=False)
        self.assertEqual([h["rule"] for h in hits], ["P3 stale"])
        self.assertTrue(hits[0].get("conflict"))

    def test_conflict_uses_up_every_push_attempt(self) -> None:
        self.test_conflict_with_moved_base_is_a_stale_hit()
        audit = gate.build_parser().parse_args(["audit", "--repo", str(self.repo), "--mode", "branch"])
        self.assertEqual(gate.cmd_audit(audit), gate.EXIT_FAIL)
        cfg = self.cfg()
        self.assertEqual(gate.load_json(gate.branch_dir(self.repo) / "attempts.json", {})["push"],
                         cfg["caps"]["push_rounds"])
        with self.assertRaises(gate.GateError) as ctx:
            gate.check_cap(self.repo, "push", cfg)
        self.assertEqual(ctx.exception.code, gate.EXIT_CAP)

    def test_the_guards_audit_neither_checks_nor_uses_push_attempts(self) -> None:
        self.stage("build.log", "log\n")
        git(self.repo, "commit", "-q", "-m", "junk")
        cfg = self.cfg()
        for _ in range(cfg["caps"]["push_rounds"]):
            gate.count_failure(self.repo, "push")
        guard = gate.build_parser().parse_args(["audit", "--repo", str(self.repo), "--mode", "branch",
                                                "--no-freshness", "--guard"])
        self.assertEqual(gate.cmd_audit(guard), gate.EXIT_FAIL)     # judged, not refused at the cap
        self.assertEqual(gate.load_json(gate.branch_dir(self.repo) / "attempts.json", {})["push"],
                         cfg["caps"]["push_rounds"])                  # and nothing more counted

    def test_failed_fetch_leaves_freshness_unproven(self) -> None:
        hits = gate.freshness_hits(self.repo, self.cfg(), ["src/pkg/mod.py"], fetch=True)   # the repo has no origin
        self.assertEqual([h["rule"] for h in hits], ["P3 stale"])
        self.assertIn("could not fetch", hits[0]["detail"])

    def test_overlap_reruns_checks_on_the_merge(self) -> None:
        self.stage("reviewers/gates.toml", 'version = 1\n[[checks]]\nid = "docs"\nkind = "mechanical"\n'
                                          'when = ["docs/**"]\nrun = "test ! -f docs/new-on-main.md"\n')
        self.stage("docs/branch.md", "# branch\n")
        git(self.repo, "commit", "-q", "-m", "branch")
        git(self.repo, "checkout", "-q", "main")
        write(self.repo, "docs/new-on-main.md", "# main\n")
        git(self.repo, "add", "docs/new-on-main.md")
        git(self.repo, "commit", "-q", "-m", "main moved")
        git(self.repo, "checkout", "-q", "feat/x")
        hits = gate.freshness_hits(self.repo, self.cfg(), ["docs/branch.md", "reviewers/gates.toml"], fetch=False)
        self.assertEqual([h["detail"].split(" ")[0] for h in hits], ["docs"])
        self.assertFalse(any(Path(p).name == "wt" for p in Path(tempfile.gettempdir()).glob("gate-wt-*/wt")))

    def test_plan_stamp(self) -> None:
        cfg = self.cfg()
        plan = Path(self.tmp.name) / "plan.md"
        plan.write_text("| file | role |\n| --- | --- |\n| `docs/guide.md` | doc |\n")
        self.assertEqual(gate.stamp_plan(self.repo, cfg, plan, accept=False)["verdict"], gate.VERDICT_PASS)  # lite
        plan.write_text("| file | role |\n| --- | --- |\n| `src/pkg/mod.py` | code |\n")
        with self.assertRaises(gate.GateError):
            gate.stamp_plan(self.repo, cfg, plan, accept=False)                # nothing reviewed this text
        stage_dir = gate.branch_dir(self.repo) / "plan"
        _, rdir = gate.next_round(stage_dir)
        seed = gate.sha256_file(plan)
        gate.save_json(rdir / "ingest.json", {"round": rdir.name, "tree": seed, "canary": {"result": "caught"},
                                              "results": [{"id": "plan", "status": "clean", "findings": []}]})
        stamp = gate.stamp_plan(self.repo, cfg, plan, accept=False)
        self.assertEqual((stamp["verdict"], stamp["tier"]), (gate.VERDICT_PASS, "standard"))
        self.assertEqual(gate.run_info(self.repo)["plan_tier"], "standard")

    def test_plan_approve_after_a_reviewed_round(self) -> None:
        cfg = self.cfg()
        plan = Path(self.tmp.name) / "plan.md"
        plan.write_text("| file | role |\n| --- | --- |\n| `src/pkg/mod.py` | code |\n")
        token = gate.TOKENS_DIR / "approve-plan.json"
        gate.TOKENS_DIR.mkdir(parents=True, exist_ok=True)
        token.write_text(json.dumps({"created": time.time(), "session": "s"}))
        with self.assertRaises(gate.GateError):
            gate.stamp_plan(self.repo, cfg, plan, accept=False, approve=True)        # no round reviewed it yet
        stage_dir = gate.branch_dir(self.repo) / "plan"
        _, rdir = gate.next_round(stage_dir)
        finding = {"id": "P5", "severity": "blocker", "file": "plan.md", "line": 1, "quote": "x", "why": "w",
                   "state": "open"}
        gate.save_json(rdir / "ingest.json", {"round": rdir.name, "tree": gate.sha256_file(plan),
                                              "canary": {"result": "caught"},
                                              "results": [{"id": "plan", "status": "findings", "findings": [finding]}]})
        plan.write_text(plan.read_text() + "| `src/pkg/other.py` | consumer |\n")    # the agent's fix
        token.unlink()
        with self.assertRaises(gate.GateError):
            gate.stamp_plan(self.repo, cfg, plan, accept=False, approve=True)        # the user armed nothing
        token.write_text(json.dumps({"created": time.time(), "session": "s"}))
        stamp = gate.stamp_plan(self.repo, cfg, plan, accept=False, approve=True)
        self.assertEqual((stamp["verdict"], stamp["approved_after"]), (gate.VERDICT_APPROVED, rdir.name))
        self.assertFalse(token.exists())
        self.assertEqual(stamp["open_blocking"], [])
        self.assertEqual([f["id"] for f in stamp["fixed_and_approved"]], ["P5"])

    def test_plan_approve_refused_without_a_caught_canary(self) -> None:
        cfg = self.cfg()
        plan = Path(self.tmp.name) / "plan.md"
        plan.write_text("| file | role |\n| --- | --- |\n| `src/pkg/mod.py` | code |\n")
        _, rdir = gate.next_round(gate.branch_dir(self.repo) / "plan")
        gate.save_json(rdir / "ingest.json", {"round": rdir.name, "tree": gate.sha256_file(plan),
                                              "canary": {"result": "missed"},
                                              "results": [{"id": "plan", "status": "clean", "findings": []}]})
        gate.TOKENS_DIR.mkdir(parents=True, exist_ok=True)
        (gate.TOKENS_DIR / "approve-plan.json").write_text(json.dumps({"created": time.time(), "session": "s"}))
        with self.assertRaises(gate.GateError):
            gate.stamp_plan(self.repo, cfg, plan, accept=False, approve=True)

    def test_approve_is_plan_only(self) -> None:
        args = gate.build_parser().parse_args(["stamp", "--repo", str(self.repo), "--stage", "review", "--approve"])
        with self.assertRaises(gate.GateError):
            gate.cmd_stamp(args)

    def test_reviewer_with_missing_files_refused(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        args = gate.build_parser().parse_args(["review-prepare", "--repo", str(self.repo)])
        with self.assertRaises(gate.GateError):
            gate.cmd_review_prepare(args)

    def test_uncalibrated_reviewer_warns_and_runs(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        gate.REVIEWERS_DIR.mkdir(parents=True)
        for r in gate.all_reviewers(self.cfg(), self.repo):
            if r.origin == "shared":
                r.source[0].write_text(f"---\nname: review-{r.id}\n---\nbody\n")
        args = gate.build_parser().parse_args(["review-prepare", "--repo", str(self.repo)])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.cmd_review_prepare(args), gate.EXIT_OK)
        self.assertIn("uncalibrated", err.getvalue())

    def test_calibration_edited_after_prepare_is_stale(self) -> None:
        write(gate.REVIEWERS_DIR, "polish.md", "lean v1\n")
        run_dir = Path(self.tmp.name) / "cal"
        run_dir.mkdir()
        lean = next(r for r in gate.all_reviewers(self.cfg(), self.repo) if r.id == "polish")
        entries = [{"id": "polish", "name": f"polish-{v}", "variant": v, "out": str(run_dir / f"{v}.out.md"),
                    "fixture": str(run_dir), "workspace": str(run_dir), "sha": gate.calibration_hash(lean)}
                   for v in ("caught", "clean")]
        gate.save_json(run_dir / "dispatch.json", {"repo": str(self.repo), "dispatch": entries})
        write(gate.REVIEWERS_DIR, "polish.md", "lean v2\n")       # edited between prepare and score
        args = gate.build_parser().parse_args(["calibrate-score", "--repo", str(self.repo), "--run-dir", str(run_dir)])
        self.assertEqual(gate.cmd_calibrate_score(args), gate.EXIT_FAIL)
        scores = gate.load_json(run_dir / "scores.json", {})
        self.assertEqual(scores["polish"]["caught"]["result"], "stale")
        self.assertIsNone(gate.load_json(gate.calibration_file(lean, self.repo), {}).get("polish"))

    def project_reviewer(self):
        write(self.repo, "reviewers/gates.toml", 'version = 1\n[[reviewer]]\nid = "guide-rules"\nstage = "logic"\n'
              'kind = "rules"\nrules = ["docs/guide.md"]\napplies_to = ["docs/**"]\n')
        return next(r for r in gate.all_reviewers(self.cfg(), self.repo) if r.id == "guide-rules")

    def test_project_calibration_is_kept_outside_the_repo(self) -> None:
        path = gate.calibration_file(self.project_reviewer(), self.repo)
        self.assertEqual(path, gate.state_root(self.repo) / "calibrated.json")
        self.assertNotIn(self.repo, path.parents)

    def test_an_in_repo_record_still_counts(self) -> None:
        r = self.project_reviewer()
        self.assertEqual(gate.calibration_problem(r, self.repo), "never calibrated")
        gate.save_json(gate.legacy_calibration_file(self.repo), {r.id: {"sha": gate.calibration_hash(r)}})
        self.assertIsNone(gate.calibration_problem(r, self.repo))

    def test_every_text_that_passed_stays_calibrated(self) -> None:
        r = self.project_reviewer()
        gate.save_json(gate.calibration_file(r, self.repo), {r.id: {"sha": "b", "shas": ["b", gate.calibration_hash(r)]}})
        self.assertIsNone(gate.calibration_problem(r, self.repo))
        gate.save_json(gate.calibration_file(r, self.repo), {r.id: {"sha": "b", "shas": ["b"]}})
        self.assertEqual(gate.calibration_problem(r, self.repo), "edited since its last calibration")

    def test_a_new_model_needs_a_new_calibration(self) -> None:
        r = self.project_reviewer()
        gate.save_json(gate.calibration_file(r, self.repo), {r.id: {"sha": gate.calibration_hash(r), "model": "strong"}})
        self.assertIn("model changed", gate.calibration_problem(r, self.repo))
        gate.save_json(gate.calibration_file(r, self.repo), {r.id: {"sha": gate.calibration_hash(r), "model": r.model}})
        self.assertIsNone(gate.calibration_problem(r, self.repo))

    def test_a_passing_calibration_is_written_outside_the_repo(self) -> None:
        r = self.project_reviewer()
        gate.save_json(gate.legacy_calibration_file(self.repo), {r.id: {"sha": "older"}})
        run_dir = Path(self.tmp.name) / "cal"
        entries = []
        for v in ("caught", "clean"):
            write(run_dir, f"{v}.out.md", "report\n")
            entries.append({"id": r.id, "name": f"{r.id}-{v}", "variant": v, "out": str(run_dir / f"{v}.out.md"),
                            "fixture": str(run_dir / v), "workspace": str(run_dir), "sha": gate.calibration_hash(r)})
        gate.save_json(run_dir / "dispatch.json", {"repo": str(self.repo), "dispatch": entries})
        args = gate.build_parser().parse_args(["calibrate-score", "--repo", str(self.repo), "--run-dir", str(run_dir)])
        by_variant = lambda _text, fixture, _rid, _ws: {"result": Path(fixture).name, "false_positives": 0}
        with mock.patch.object(gate, "score_fixture", side_effect=by_variant), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.cmd_calibrate_score(args), gate.EXIT_OK)
        record = gate.load_json(gate.calibration_file(r, self.repo), {})[r.id]
        self.assertEqual(record["shas"], sorted({"older", gate.calibration_hash(r)}))
        self.assertEqual(gate.load_json(gate.legacy_calibration_file(self.repo), {}), {r.id: {"sha": "older"}})

    def test_baseline_verdicts(self) -> None:
        self.stage("reviewers/gates.toml", 'version = 1\n[[checks]]\nid = "t"\nkind = "unit"\nwhen = ["**"]\n'
                                          'run = "test ! -f src/pkg/new.py || { echo FAILED tests/t.py::a; exit 1; }"\n')
        self.stage("src/pkg/new.py", "X = 1\n")
        run = gate.build_parser().parse_args(["checks-run", "--repo", str(self.repo)])
        base = gate.build_parser().parse_args(["checks-run", "--repo", str(self.repo), "--baseline", "t"])
        self.assertEqual(gate.cmd_checks_run(run), gate.EXIT_FAIL)
        self.assertEqual(gate.cmd_checks_run(base), gate.EXIT_FAIL)           # base is green: yours
        self.stage("reviewers/gates.toml", 'version = 1\n[[checks]]\nid = "t"\nkind = "unit"\nwhen = ["**"]\n'
                                          'run = "echo FAILED tests/t.py::a; exit 1"\n')
        gate.reset_attempt(self.repo, "checks")
        self.assertEqual(gate.cmd_checks_run(run), gate.EXIT_FAIL)
        self.assertEqual(gate.cmd_checks_run(base), gate.EXIT_FAIL)           # same failure on base: still blocks
        state = gate.load_json(gate.branch_dir(self.repo) / "checks" / "state.json", {})
        self.assertEqual(state["t"]["status"], "fail")
        self.assertIn("also fails on base", state["t"]["note"])
        with self.assertRaises(gate.GateError):
            gate.stamp_checks(self.repo, self.cfg(), accept=False)

    def test_baseline_needs_the_failure_on_this_tree(self) -> None:
        self.stage("reviewers/gates.toml", 'version = 1\n[[checks]]\nid = "t"\nkind = "unit"\nwhen = ["**"]\n'
                                          'run = "echo FAILED tests/t.py::a; exit 1"\n')
        parse = gate.build_parser().parse_args
        self.assertEqual(gate.cmd_checks_run(parse(["checks-run", "--repo", str(self.repo)])), gate.EXIT_FAIL)
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")          # the tree moved on
        with self.assertRaises(gate.GateError):
            gate.cmd_checks_run(parse(["checks-run", "--repo", str(self.repo), "--baseline", "t"]))


class GateViewTest(RepoCase):
    def test_dashboard_and_status_line_follow_the_rounds(self) -> None:
        import gate_view

        stage_dir = gate.branch_dir(self.repo) / "review"
        _, rdir = gate.next_round(stage_dir)
        finding = {"id": "C1", "severity": "blocker", "file": "a.py", "line": 3, "why": "w", "state": "open"}
        gate.save_json(rdir / "ingest.json", {"canary": {"result": "caught"}, "results": [
            {"id": "correctness", "status": "findings", "findings": [finding, {**finding, "severity": "nit"}]}]})
        _, running = gate.next_round(stage_dir)
        gate.save_json(running / "dispatch.json", {"dispatch": [{"id": "lean", "name": "lean"}]})
        page = gate_view.write(gate.branch_dir(self.repo), {"plan": 2, "review": 3})
        view = json.loads((page.parent / gate_view.VIEW_FILE).read_text())
        first, second = view["stages"]["review"]
        self.assertEqual((first["counts"]["blocker"], len(first["open"]), second["status"]), (1, 1, "running"))
        text = page.read_text()
        self.assertIn("round-1", text)
        self.assertIn("3 &middot; Found, not handled yet", text)
        self.assertIn("No divergence log registered", text)
        self.assertEqual(gate_view.status_line(view), "gate feat/x · code review r2 · attempts 0/3 · reviewing (1)")
        gate.save_json(running / "ingest.json", {"canary": {"result": "caught"}, "results": [
            {"id": "lean", "status": "clean", "findings": []}]})
        view = gate_view.summary(gate.branch_dir(self.repo), {"review": 3})
        self.assertTrue(gate_view.status_line(view).endswith("code review r2 · attempts 0/3 · clean"))

    def test_at_the_cap_the_page_asks_and_explains_each_issue_plainly(self) -> None:
        import gate_view

        bdir = gate.branch_dir(self.repo)
        _, rdir = gate.next_round(bdir / "review")
        finding = {"id": "C2", "severity": "should-fix", "file": "a.py", "line": 3, "why": "tech", "fix": "f",
                   "state": "open", "plain": "The page freezes."}
        gate.save_json(rdir / "ingest.json", {"canary": {"result": "caught"}, "seconds": 600, "results": [
            {"id": "correctness", "status": "findings", "findings": [finding, {**finding, "severity": "nit", "plain": "A name is odd."}]}]})
        gate.save_json(bdir / "attempts.json", {"review": 1})
        self.assertIn("Not committed yet: the agent is fixing this", gate_view.render(gate_view.summary(bdir, {"review": 3})))
        with contextlib.redirect_stdout(io.StringIO()):
            gate.main(["note", "--repo", str(self.repo), "--finding", "correctness#0", "--done", "Fixed: no freeze."])
        text = gate_view.render(gate_view.summary(bdir, {"review": 1}))
        for part in ("Not committed: your choice is needed", "The page freezes.", "What the agent did:</b> Fixed: no freeze.",
                     "#accept-gate", "about 10 minutes", "A reviewer checks the agent's fixes.", "handled, waiting for a reviewer to re-check it", "2 &middot; Fixes made to the code", "Waiting for a reviewer to confirm",
                     "<span class='badge nice'>Nice to have</span>A name is odd.", "1 important"):
            self.assertIn(part, text)
        with self.assertRaises(gate.GateError):
            gate.cmd_note(gate.build_parser().parse_args(["note", "--repo", str(self.repo), "--finding", "correctness#5"]))

    def _set_aside_page(self, severity: str, body: str, file: str = "src/pkg/set_aside.py") -> str:
        """The page once a finding on `BAD = 1` was rejected, with `body` staged and stamped as the current code."""
        import gate_view

        self.stage("src/pkg/set_aside.py", body)
        finding = {"id": "R1", "severity": severity, "file": file, "line": 1, "quote": "BAD = 1",
                   "why": "w", "fix": "f", "plain": "The judge is wrong.", "state": "rejected", "key": "k1"}
        self._ingest([{"id": "lean", "status": "clean", "error": "", "findings": [finding]}])
        tree = gate.index_tree(self.repo)
        for stage in ("review", "checks"):
            gate.save_json(gate.tree_dir(self.repo, tree) / f"{stage}.json", {"verdict": "PASS"})
        return gate_view.render(gate_view.summary(gate.branch_dir(self.repo), {"review": 3},
                                                  gate.view_trees(self.repo), self.repo))

    def test_a_serious_finding_the_agent_set_aside_stops_all_passed(self) -> None:
        text = self._set_aside_page("blocker", "BAD = 1\n")
        self.assertIn("Review and tests passed, but the agent did not fix 1 serious problem</h2>", text)
        self.assertIn("<li>The judge is wrong.</li>", text)
        self.assertIn("<b>fix them</b>", text)
        self.assertNotIn("All passed", text)

    def test_a_set_aside_box_claims_no_pass_nothing_earned(self) -> None:
        import gate_view

        self.stage("src/pkg/set_aside.py", "BAD = 1\n")
        finding = {"id": "R1", "severity": "blocker", "file": "src/pkg/set_aside.py", "line": 1, "quote": "BAD = 1",
                   "why": "w", "fix": "f", "plain": "The judge is wrong.", "state": "rejected", "key": "k1"}
        self._ingest([{"id": "lean", "status": "clean", "error": "", "findings": [finding]}])
        text = gate_view.render(gate_view.summary(gate.branch_dir(self.repo), {"review": 3},
                                                  gate.view_trees(self.repo), self.repo))
        self.assertIn("<h2>The agent did not fix 1 serious problem</h2>", text)
        self.assertIn("The remaining checks are still to run.", text)
        self.assertNotIn("Committed, but", text)
        self.assertNotIn("Review and tests passed", text)

    def test_a_stamp_or_a_skip_token_ends_the_stop(self) -> None:
        import gate_view

        self.stage("src/pkg/accepted.py", "x = 1\n")
        finding = {"id": "C1", "severity": "blocker", "file": "src/pkg/accepted.py", "line": 1, "quote": "x = 1",
                   "why": "w", "fix": "f", "plain": "Wrong.", "state": "open", "key": "k1"}
        self._ingest([{"id": "lean", "status": "findings", "error": "", "findings": [finding]}])
        bdir, tree = gate.branch_dir(self.repo), gate.index_tree(self.repo)
        page = lambda: gate_view.render(gate_view.summary(bdir, {"review": 3}, gate.view_trees(self.repo), self.repo))
        self.assertIn("the agent is fixing this", page())
        gate.save_json(gate.tree_dir(self.repo, tree) / "checks.json", {"verdict": "PASS"})
        gate.save_json(gate.tree_dir(self.repo, tree) / "review.json", {"verdict": "ACCEPTED"})
        self.assertNotIn("the agent is fixing this", page())
        self.assertIn("You accepted or skipped a check &mdash; ready to commit", page())
        self.assertNotIn("All passed", page())
        (gate.tree_dir(self.repo, tree) / "review.json").unlink()
        self.assertIn("the agent is fixing this", page())
        gate.save_json(gate.tree_dir(self.repo, tree) / gate.USER_SKIP_FILE, {"kind": "skip-review"})
        self.assertIn("You accepted or skipped a check", page())
        self.assertNotIn("All passed", page())
        self.assertIn("<li>AI code review: skipped by you</li>", page())

    def test_a_set_aside_finding_on_a_deleted_file_clears(self) -> None:
        self.assertIn("All passed", self._set_aside_page("blocker", "BAD = 1\n", file="src/pkg/deleted_since.py"))

    def _rejected_plan_finding_page(self, severity: str) -> str:
        """The page once a plan finding was rejected, with no code stamped yet."""
        import gate_view

        bdir = gate.branch_dir(self.repo)
        _, rdir = gate.next_round(bdir / "plan")
        finding = {"id": "P1", "severity": severity, "file": "plan.md", "line": 1, "quote": "not in any file", "why": "w",
                   "fix": "f", "plain": "A plan problem.", "state": "rejected", "key": "k9"}
        gate.save_json(rdir / "ingest.json", {"canary": {"result": "unavailable"}, "seconds": 1,
                                              "results": [{"id": "plan", "status": "clean", "error": "", "findings": [finding]}]})
        return gate_view.render(gate_view.summary(bdir, {"plan": 2}, None, self.repo))

    def test_a_rejected_plan_finding_stays_listed_in_section_5_and_leaves_the_box_alone(self) -> None:
        text = self._rejected_plan_finding_page("blocker")
        self.assertIn("1 &middot; Changes to the original plan <span class=muted>(0)</span>", text)
        self.assertIn("5 &middot; Findings the agent did not fix <span class=muted>(1)</span>", text)
        self.assertNotIn("Set aside earlier, fixed since", text)
        self.assertNotIn("did not fix 1 serious problem", text)

    def test_a_rejected_plan_finding_is_never_worded_as_no_problems(self) -> None:
        text = self._rejected_plan_finding_page("blocker")
        self.assertIn("Nothing is blocking so far &mdash; not ready to commit yet", text)
        self.assertIn("clean, 1 plan finding set aside", text)
        self.assertNotIn("No problems found so far", text)

    def test_a_rejected_plan_nice_to_have_is_not_counted(self) -> None:
        self.assertNotIn("plan finding set aside", self._rejected_plan_finding_page("nit"))

    def test_the_stamp_names_the_model_tier_the_reviewer_really_ran_at(self) -> None:
        reviewers = {r.id: r for r in gate.all_reviewers(self.cfg(), self.repo)}
        stage_dir = Path(self.tmp.name) / "stage"
        gate.save_json(stage_dir / "round-1" / "dispatch.json", {"dispatch": [{"id": "polish", "model_tier": "strong"}]})
        state = {"polish": {"round": "round-1"}}
        self.assertEqual(gate.last_dispatched_model(reviewers["polish"], gate.TIER_FULL, stage_dir, state), "strong")
        self.assertEqual(gate.last_dispatched_model(reviewers["polish"], gate.TIER_FULL, stage_dir, {}), "standard")

    def test_a_plan_stop_under_the_cap_offers_the_approval_it_may_be_waiting_for(self) -> None:
        import gate_view

        bdir = gate.branch_dir(self.repo)
        _, rdir = gate.next_round(bdir / "plan")
        finding = {"id": "P1", "severity": "blocker", "file": "plan.md", "line": 1, "quote": "q", "why": "w", "fix": "f",
                   "plain": "A plan problem.", "state": "open", "key": "k9"}
        gate.save_json(rdir / "ingest.json", {"canary": {"result": "unavailable"}, "seconds": 1,
                                              "results": [{"id": "plan", "status": "findings", "error": "", "findings": [finding]}]})
        self.assertIn("type <b>#approve-plan</b>", gate_view.render(gate_view.summary(bdir, {"plan": 2})))

    def test_run_again_promises_a_stronger_model_only_for_unread_files(self) -> None:
        import gate_view

        self._ingest([{"id": "polish", "status": "invalid", "error": "output not saved: x", "findings": []}])
        bdir = gate.branch_dir(self.repo)
        gate.save_json(bdir / "attempts.json", {"review": 1})
        text = gate_view.render(gate_view.summary(bdir, {"review": 1}))
        self.assertIn("A reviewer whose report could not be used runs again.", text)
        self.assertIn("<b>polish</b> &mdash; its report was not saved", text)
        self.assertNotIn("stronger AI model", text)

    def test_a_plan_stop_promises_no_stronger_model(self) -> None:
        import gate_view

        bdir = gate.branch_dir(self.repo)
        _, rdir = gate.next_round(bdir / "plan")
        gate.save_json(rdir / "ingest.json", {"canary": {"result": "unavailable"}, "seconds": 60, "results": [
            {"id": "plan", "status": "invalid", "error": "did not report reading ['plan.md']", "findings": []}]})
        gate.save_json(bdir / "attempts.json", {"plan": 2})
        text = gate_view.render(gate_view.summary(bdir, {"plan": 2}))
        self.assertIn("That reviewer tries again.", text)
        self.assertNotIn("stronger AI model", text)

    def test_a_report_on_another_tree_is_explained_in_plain_words(self) -> None:
        import gate_view

        report = '```json\n{"reviewer": "r", "tree": "a", "checked": [], "findings": []}\n```'
        wrong = gate.judge_output(report, "r", [], self.repo, "b", self.repo, {}, "b")
        self.assertEqual(gate_view._plain_error(wrong["error"]), "it reviewed a different version of the code")
        self.assertEqual(gate_view._plain_error("no JSON block"), "its report could not be read (no JSON block)")

    def test_a_missed_canary_is_named_in_the_box_the_status_line_and_the_next_step(self) -> None:
        import gate_view

        self._ingest([{"id": "lean", "status": "clean", "error": "", "findings": []}], canary="missed")
        view = gate_view.summary(gate.branch_dir(self.repo), {"review": 3})
        text = gate_view.render(view)
        self.assertIn("Not committed yet: the agent is fixing this", text)
        self.assertIn("The AI reviewers missed a test bug planted to check them", text)
        self.assertIn("<b>canary</b> &mdash; the reviewers missed the planted test bug", text)
        self.assertIn("canary missed", gate_view.status_line(view))
        self.assertIn("a missed canary", gate_view.next_step({**view, "plan_stamp": "PASS"}, "p.md"))

    def test_under_the_cap_an_invalid_report_sends_the_next_session_back_to_the_review(self) -> None:
        import gate_view

        self._ingest([{"id": "polish", "status": "invalid", "error": "no JSON block", "findings": []}])
        view = gate_view.summary(gate.branch_dir(self.repo), {"review": 3})
        step = gate_view.next_step({**view, "plan_stamp": "PASS"}, "p.md")
        self.assertIn("round-1 did not pass (an invalid report or a missed canary)", step)
        self.assertNotIn("Stop and ask the user", step)

    def test_a_skipped_check_is_not_shown_as_failed(self) -> None:
        import gate_view

        bdir = gate.branch_dir(self.repo)
        gate.save_json(bdir / "checks" / "state.json", {"unit": {"status": "pass", "tree": "abc", "seconds": 1},
                                                        "targeted": {"status": "skipped", "reason": "no mapped tests"}})
        view = gate_view.summary(bdir, {"review": 3})
        self.assertTrue(gate_view.status_line(view).endswith("checks pass (2) @abc"), gate_view.status_line(view))
        self.assertIn("- Checks: passed on code version abc.", gate_view.handoff(view, self.repo, {}))
        gate.save_json(bdir / "checks" / "state.json", {"targeted": {"status": "skipped", "reason": "no mapped tests"}})
        view = gate_view.summary(bdir, {"review": 3})
        self.assertTrue(gate_view.status_line(view).endswith("checks pass (1)"), gate_view.status_line(view))
        self.assertIn("- Checks: passed.", gate_view.handoff(view, self.repo, {}))

    def test_a_failed_push_audit_says_not_pushed_and_names_the_rule(self) -> None:
        import gate_view

        gate.save_json(gate.tree_dir(self.repo, gate.head_tree(self.repo)) / "audit-branch.json",
                       {"hits": [{"rule": "P2 secret", "file": "x.py", "detail": "a key"}]})
        text = gate_view.render(gate_view.summary(gate.branch_dir(self.repo), {"push": 2},
                                                  gate.view_trees(self.repo), self.repo))
        self.assertIn("Not pushed yet: the agent is fixing this", text)
        self.assertIn("1 push rule failed.", text)
        self.assertIn("<b>P2 secret</b> &mdash; x.py: a key", text)

    def test_a_stage_out_of_tries_with_no_recorded_failure_still_says_so(self) -> None:
        import gate_view

        bdir = gate.branch_dir(self.repo)
        for stage, subject in (("checks", "Not committed"), ("push", "Not pushed")):
            with self.subTest(stage):
                gate.save_json(bdir / "attempts.json", {stage: 2})
                text = gate_view.render(gate_view.summary(bdir, {stage: 2}, gate.view_trees(self.repo), self.repo))
                self.assertIn(f"{subject}: your choice is needed", text)
                self.assertIn("failed all 2 of its tries", text)

    def test_only_an_unread_files_report_moves_a_reviewer_up_a_model_tier(self) -> None:
        reviewers = {r.id: r for r in gate.all_reviewers(self.cfg(), self.repo)}
        stage_dir = Path(self.tmp.name) / "stage"
        report = lambda error: gate.save_json(stage_dir / "round-1" / "ingest.json", {
            "results": [{"id": "polish", "status": "invalid", "error": error, "findings": []}]})
        state = {"polish": {"round": "round-1"}}
        model = lambda rid: gate.dispatch_model(reviewers[rid], gate.TIER_FULL, stage_dir, state)
        self.assertEqual(model("polish") if not report("did not report reading ['a.py']") else "", "strong")
        self.assertEqual(model("polish") if not report("no JSON block") else "", "standard")
        self.assertEqual(gate.dispatch_model(reviewers["polish"], gate.TIER_FULL, stage_dir, {}), "standard")

    def test_unittest_failures_are_named_and_its_count_line_is_not(self) -> None:
        output = ("FAIL: test_one (a.B.test_one)\nERROR: test_two (a.B.test_two)\nFAILED (failures=1, errors=1)\n"
                  "FAILED tests/test_a.py::test_three - boom\nERROR tests/test_b.py\n"
                  "ERROR: file or directory not found: tests/old.py\n")
        self.assertEqual(gate.FAILED_TEST.findall(output),
                         ["test_one", "test_two", "tests/test_a.py::test_three", "tests/test_b.py"])

    def test_an_escaped_pipe_stays_in_its_divergence_cell(self) -> None:
        import gate_view

        log = Path(self.tmp.name) / "pipes.md"
        log.write_text("| # | tag | plan said | did instead | why |\n|---|---|---|---|---|\n"
                       "| 3 | [LOGIC] | x: str \\| None | kept x: str | caller passes None |\n")
        self.assertEqual(gate_view.divergences(str(log))[0],
                         {"n": "3", "tag": "[LOGIC]", "plan": "x: str | None", "did": "kept x: str", "why": "caller passes None"})

    def test_a_lost_divergence_log_is_not_reported_as_no_log(self) -> None:
        import gate_view

        gone = str(Path(self.tmp.name) / "gone.md")
        gate.save_json(gate.branch_dir(self.repo) / "run.json", {"divergence_log": gone})
        self.assertIn(f"The divergence log file is missing: <code>{gone}</code>",
                      gate_view.render(gate_view.summary(gate.branch_dir(self.repo), {})))

    def test_one_problem_raised_on_two_lines_shows_once(self) -> None:
        import gate_view

        self.stage("src/pkg/set_aside.py", "BAD = 1\nWORSE = 2\n")
        twins = [{"id": "R1", "severity": "blocker", "file": "src/pkg/set_aside.py", "line": n, "quote": quote, "why": "w",
                  "fix": "f", "plain": "The judge is wrong.", "state": "rejected", "key": f"k{n}"}
                 for n, quote in ((1, "BAD = 1"), (2, "WORSE = 2"))]
        self._ingest([{"id": "lean", "status": "clean", "error": "", "findings": twins}])
        text = gate_view.render(gate_view.summary(gate.branch_dir(self.repo), {"review": 3},
                                                  gate.view_trees(self.repo), self.repo))
        self.assertIn("did not fix 1 serious problem</h2>", text)
        self.assertIn("<li>The judge is wrong. <i>(in 2 places)</i></li>", text)
        self.assertIn("5 &middot; Findings the agent did not fix <span class=muted>(1)</span>", text)
        self.assertIn("The same problem is also at <code>src/pkg/set_aside.py:2</code>.", text)
        self.assertEqual(len(re.findall(r"<details class='card", text)), 1)

    def test_a_set_aside_finding_clears_only_when_its_text_has_left_the_code(self) -> None:
        text = self._set_aside_page("blocker", "GOOD = 1\n")
        self.assertIn("All passed", text)
        self.assertIn("5 &middot; Findings the agent did not fix <span class=muted>(0)</span>", text)
        self.assertIn("Set aside earlier, fixed since", text)

    def test_a_set_aside_nice_to_have_does_not_stop_all_passed(self) -> None:
        self.assertIn("All passed", self._set_aside_page("nit", "BAD = 1\n"))

    def test_a_set_aside_should_fix_stops_all_passed(self) -> None:
        self.assertNotIn("All passed", self._set_aside_page("should-fix", "BAD = 1\n"))

    def test_an_open_nit_is_listed_and_never_stops_the_box(self) -> None:
        import gate_view

        finding = {"id": "CV1", "severity": "nit", "file": "a.py", "line": 1, "quote": "x", "why": "w", "fix": "f",
                   "plain": "A name is odd.", "state": "open", "key": "k3"}
        self._ingest([{"id": "polish", "status": "clean", "error": "", "findings": [finding]}])
        view = gate_view.summary(gate.branch_dir(self.repo), {"review": 3})
        text = gate_view.render(view)
        self.assertIn("Nothing is blocking so far", text)
        self.assertIn("3 &middot; Found, not handled yet <span class=muted>(1)</span>", text)
        self.assertTrue(gate_view.status_line(view).endswith("clean"), gate_view.status_line(view))

    def test_an_older_bug_asks_the_user_and_is_never_shown_as_fixed(self) -> None:
        import gate_view

        finding = {"id": "C2", "severity": "should-fix", "file": "a.py", "line": 1, "quote": "x", "why": "w", "fix": "f",
                   "plain": "An old crash.", "state": "open", "key": "k4", "older": True}
        self._ingest([{"id": "correctness", "status": "clean", "error": "", "findings": [finding]}])
        self._ingest([{"id": "lean", "status": "clean", "error": "", "findings": []}])
        view = gate_view.summary(gate.branch_dir(self.repo), {"review": 3})
        text = gate_view.render(view)
        for part in ("Your decision: 1 older bug found", "<li>An old crash.</li>", "<b>fix them</b>", "<b>#accept-gate</b>"):
            self.assertIn(part, text)
        self.assertNotIn("Fixed before the next round", text)
        self.assertIn("1 older bug for you to decide", gate_view.status_line(view))
        self.assertIn("older bugs", gate_view.next_step({**view, "plan_stamp": "PASS"}, "p.md"))

    def test_each_finding_gets_one_true_label(self) -> None:
        import gate_view

        def item(state: str = "open", severity: str = "should-fix", done: str = "", older: bool = False) -> dict:
            return {"round": "round-1", "reviewer": "r", "id": "C1", "ref": "r#0", "severity": severity, "state": state, "file": "a.py", "line": 1,
                    "quote": "x", "why": "w", "fix": "f", "plain": "p", "done": done, "older": older, "key": "k"}

        nit, older = item(severity="nit"), item(older=True)
        clean, unusable, another = ("clean", []), ("invalid", []), ("clean", [], "another-reviewer")
        waiting, not_handled = "Waiting for a reviewer to confirm the fix.", "Not handled yet."
        your_decision = "An older bug this change did not cause: your decision."
        cases = {   # the reports of each round, whether the older bug still waits for the user, and the labels the page gives
            "open in the last round": ([("findings", [item()])], False, [not_handled]),
            "reported again": ([("findings", [item()]), ("findings", [item()])], False, [not_handled]),
            "no longer reported": ([("findings", [item()]), clean], False, ["Fixed before the next round, which re-checked the code."]),
            "unusable report after it": ([("findings", [item()]), unusable], False, [not_handled]),
            "only another reviewer ran after it": ([("findings", [item()]), another], False, [not_handled]),
            "fix noted, not re-checked": ([("findings", [item(done="Fixed: x")])], False, [waiting]),
            "fix noted, unusable report": ([("findings", [item(done="Fixed: x")]), unusable], False, [waiting]),
            "fix noted, only another reviewer ran": ([("findings", [item(done="Fixed: x")]), another], False, [waiting]),
            "fix noted, re-checked": ([("findings", [item(done="Fixed: x")]), clean], False, ["A later round re-checked the code."]),
            "nit, reviewer ran again": ([("clean", [nit]), clean], False, ["Nice to have; whether it was fixed was not recorded."]),
            "nit, reviewer never ran again": ([("clean", [nit])], False, [not_handled]),
            "nit left by the agent": ([("clean", [item(severity="nit", done="Left: x")])], False, ["Nice to have; the agent left it as it is."]),
            "older nit": ([("clean", [item(severity="nit", older=True)])], False, [not_handled]),
            "older bug waiting, reported twice": ([("clean", [older]), ("clean", [older])], True, [your_decision]),
            "older bug cleared": ([("clean", [older]), clean], False, ["An older bug: fixed since, or you chose to leave it."]),
            "rejected": ([("clean", [item(state="rejected")])], False, []),
            "older bug rejected, raised again, waiting": ([("clean", [item(state="rejected", older=True)]),
                                                           ("clean", [item(state="reraised", older=True)])], True, [your_decision]),
            "older bug rejected, raised again, cleared": ([("clean", [item(state="rejected", older=True)]),
                                                           ("clean", [item(state="reraised", older=True)]), clean], False, []),
            "rejected, raised again, still open": ([("clean", [item(state="rejected")]), ("findings", [item(state="reraised")])],
                                                   False, [not_handled]),
            "rejected, raised again, then gone": ([("clean", [item(state="rejected")]), ("findings", [item(state="reraised")]), clean],
                                                  False, []),
        }
        for name, (reports, waits, labels) in cases.items():
            with self.subTest(name):
                stages = {"plan": [], "review": [{"name": f"round-{n}", "status": "ingested", "items": items,
                                                  "reviewers": [{"id": who[0] if who else "r", "status": status}]}
                                                 for n, (status, items, *who) in enumerate(reports, 1)]}
                rejected = any(f["state"] == "rejected" for report in reports for f in report[1])
                view = {"stages": stages, "older": [{"key": "k"}] if waits else [], "set_aside": [{"key": "k"}] if rejected else []}
                self.assertEqual(re.findall(r"Found in round-\d+\. ([^<]+)", gate_view._three_sections(view)), labels)

    def test_a_nit_left_as_it_is_is_not_listed_as_a_fix(self) -> None:
        import gate_view

        finding = {"id": "N1", "severity": "nit", "file": "a.py", "line": 1, "quote": "x", "why": "w", "fix": "f",
                   "plain": "A nit.", "state": "open", "key": "k2"}
        rdir = self._ingest([{"id": "lean", "status": "clean", "error": "", "findings": [finding]}])
        gate.save_json(rdir / gate_view.NOTES_FILE, {"lean#0": {"plain": "", "done": "Left as it is: on purpose."}})
        text = gate_view.render(gate_view.summary(gate.branch_dir(self.repo), {"review": 3}))
        self.assertIn("for your review <span class=muted>(0)</span>", text)
        self.assertIn("3 &middot; Found, not handled yet <span class=muted>(1)</span>", text)
        self.assertIn("Nice to have; the agent left it as it is.", text)

    def test_the_dashboard_lists_every_divergence_log_row(self) -> None:
        import gate_view

        log = Path(self.tmp.name) / "log.md"
        log.write_text("# Divergence log\nbase: abc\n\n| # | tag | plan said | did instead | why |\n|---|---|---|---|---|\n"
                       "| 1 | [LOGIC] | `a.py` only | also `b.py` | b calls a |\n"
                       "| 2 | [MECH] | x | y | because p | q |\n")
        gate.save_json(gate.branch_dir(self.repo) / "run.json", {"divergence_log": str(log)})
        view = gate_view.summary(gate.branch_dir(self.repo), {"review": 3})
        self.assertEqual(view["divergences"][1], {"n": "2", "tag": "[MECH]", "plan": "x", "did": "y", "why": "because p | q"})
        text = gate_view.render(view)
        self.assertIn("4 &middot; Divergences from the plan <span class=muted>(2)</span>", text)
        self.assertIn("<span class='badge important'>#1 LOGIC</span>also <code>b.py</code></summary>", text)
        self.assertIn("<dt>Plan said</dt><dd><code>a.py</code> only</dd><dt>Did instead</dt><dd>also <code>b.py</code></dd>"
                      "<dt>Why</dt><dd>b calls a</dd>", text)
        self.assertNotIn("<table><tr><th>#</th>", text)
        self.assertLess(text.index("id=divergences"), text.index("id=more"))
        log.write_text("# Divergence log\nbase: abc\n\n| # | tag | plan said | did instead | why |\n|---|---|---|---|---|\n")
        self.assertIn("None recorded in", gate_view.render(gate_view.summary(gate.branch_dir(self.repo), {})))
        log.unlink()
        self.assertIsNone(gate_view.divergences(str(log)))

    def _ingest(self, results: list, seconds: int = 60, canary: str = "unavailable") -> Path:
        _, rdir = gate.next_round(gate.branch_dir(self.repo) / "review")
        gate.save_json(rdir / "ingest.json", {"canary": {"result": canary}, "seconds": seconds, "results": results})
        return rdir

    def test_invalid_reports_at_the_cap_stop_the_commit_and_say_why(self) -> None:
        import gate_view

        self._ingest([{"id": "polish", "status": "invalid", "error": "did not report reading ['a.py', 'b.py']",
                       "findings": []}, {"id": "tests", "status": "clean", "error": "", "findings": []}])
        bdir = gate.branch_dir(self.repo)
        gate.save_json(bdir / "attempts.json", {"review": 3})
        view = gate_view.summary(bdir, {"review": 3})
        text = gate_view.render(view)
        for part in ("Not committed: your choice is needed",
                     "<strong>What happened:</strong> AI reviewers check the code before it can be committed. "
                     "1 of them did not finish, so the check is not complete. "
                     "A report that did not finish is not a problem with your code. Anything serious still open is listed below.",
                     "<b>run again</b> (recommended) &mdash; That reviewer tries again, on a stronger AI model when there is one",
                     "<b>polish</b> &mdash; did not read 2 of its files to the end <i>(not a code problem)</i>",
                     "#accept-gate", "#abort"):
            self.assertIn(part, text)
        self.assertNotIn("Nothing for you to decide", text)
        self.assertNotIn("<b>tests</b>", text)
        self.assertTrue(gate_view.status_line(view).endswith("not passed: 1 invalid reports"))
        self.assertIn("Stop and ask the user", gate_view.next_step({**view, "plan_stamp": "PASS"}, "p.md"))

    def test_a_red_check_names_its_log_and_last_failing_test(self) -> None:
        import gate_view

        bdir = gate.branch_dir(self.repo)
        gate.save_json(bdir / "checks" / "state.json", {"unit": {
            "status": "fail", "tree": "c" * 40, "log": "/s/unit.log", "seconds": 2,
            "failed_ids": ["tests/test_a.py::test_one", "tests/test_a.py::test_two"]}})
        gate.save_json(bdir / "attempts.json", {"checks": 1})
        text = gate_view.render(gate_view.summary(bdir, {"checks": 3}))
        for part in ("Not committed yet: the agent is fixing this", "1 automatic test failed.", "Nothing is needed from you.",
                     "<b>unit</b> &mdash; red on tree <code>cccccccccc</code>: last failing test "
                     "<code>tests/test_a.py::test_two</code>, log <code>/s/unit.log</code>"):
            self.assertIn(part, text)
        self.assertNotIn("Nothing for you to decide", text)
        gate.save_json(bdir / "attempts.json", {"checks": 3})
        stopped = gate_view.render(gate_view.summary(bdir, {"checks": 3}))
        for part in ("Not committed: your choice is needed", "The agent fixes the failure, then the tests run again.", "#accept-gate"):
            self.assertIn(part, stopped)

    def test_all_passed_says_ready_to_commit_then_committed(self) -> None:
        import gate_view

        self._ingest([{"id": "tests", "status": "clean", "error": "", "findings": []}])
        index, head = gate.index_tree(self.repo), gate.head_tree(self.repo)
        self.assertEqual(index, head)
        for stage in ("review", "checks"):
            gate.save_json(gate.tree_dir(self.repo, index) / f"{stage}.json", {"verdict": "PASS"})
        bdir = gate.branch_dir(self.repo)
        text = gate_view.render(gate_view.summary(bdir, {"review": 3}, {"index": index, "head": head}))
        self.assertIn("All passed &mdash; committed", text)
        self.assertIn(f"<li>AI code review: passed on the current files (version <code>{index[:10]}</code>)</li>", text)
        self.assertIn("<li>Final check before pushing: not run yet</li>", text)
        self.stage("src/pkg/new.py", "x = 1\n")
        staged = gate.index_tree(self.repo)
        for stage in ("review", "checks"):
            gate.save_json(gate.tree_dir(self.repo, staged) / f"{stage}.json", {"verdict": "PASS"})
        text = gate_view.render(gate_view.summary(bdir, {"review": 3}, gate.view_trees(self.repo)))
        self.assertIn("All passed &mdash; ready to commit", text)
        gate.save_json(gate.tree_dir(self.repo, staged) / "audit-staged.json",
                       {"hits": [{"rule": "H1 unplanned", "file": "src/pkg/new.py", "detail": "not in the plan"}]})
        text = gate_view.render(gate_view.summary(bdir, {"review": 3}, gate.view_trees(self.repo)))
        self.assertIn("<ul><li><b>H1 unplanned</b> &mdash; src/pkg/new.py: not in the plan", text)

    def test_rejected_findings_show_with_the_agents_reason(self) -> None:
        import gate_view

        finding = {"id": "L1", "severity": "should-fix", "file": "a.py", "line": 1, "why": "dead code",
                   "state": "rejected", "key": "k1"}
        rdir = self._ingest([{"id": "lean", "status": "clean", "error": "", "findings": [finding]}])
        gate.save_json(rdir.parent / gate_view.REJECTIONS_FILE, {"k1": {"round": rdir.name, "cite": "a.py:9",
                                                                       "reason": "it is called at a.py:9"}})
        self._ingest([{"id": "lean", "status": "clean", "error": "", "findings": []}])
        text = gate_view.render(gate_view.summary(gate.branch_dir(self.repo), {"review": 3}))
        self.assertIn("5 &middot; Findings the agent did not fix <span class=muted>(1)</span>", text)
        self.assertIn("The agent's reason: it is called at a.py:9 (cites <code>a.py:9</code>).", text)
        # Section 5 is the only place it shows, so the page has one card for it, and its id names that section.
        ids = re.findall(r"<details class='card[^']*' id='([^']+)'", text)
        self.assertEqual(ids, ["rejected-round-1-lean#0"])

    def test_main_refreshes_the_view_and_never_fails_on_it(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            gate.main(["status", "--repo", str(self.repo)])
        page = gate.branch_dir(self.repo) / "dashboard.html"
        self.assertTrue(page.exists())
        self.assertIn(f"dashboard (reloads itself): {page}", err.getvalue())
        self.assertNotIn("dashboard", out.getvalue())   # stdout stays pasteable
        self.assertIsNone(gate.refresh_view("/no/such/repo"))

    def test_the_dashboard_line_gives_the_windows_path_under_wsl(self) -> None:
        page = Path("/home/u/state/dashboard.html")
        with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu-22.04"}):
            self.assertIn(r"(Windows: \\wsl.localhost\Ubuntu-22.04\home\u\state\dashboard.html)", gate.dashboard_line(page))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gate.dashboard_line(page), f"dashboard (reloads itself): {page}")


class HandoffTest(RepoCase):
    def _register(self) -> None:
        side = Path(self.tmp.name) / "scratch"
        side.mkdir()
        for name, text in (("request.md", "build x"), ("plan.md", "# Plan\n"), ("log.md", "| # |\n")):
            (side / name).write_text(text)
        with contextlib.redirect_stdout(io.StringIO()):
            gate.main(["init", "--repo", str(self.repo), "--request", str(side / "request.md"),
                       "--plan", str(side / "plan.md"), "--divergence-log", str(side / "log.md")])

    def test_the_handoff_moves_the_files_into_the_worktree_and_says_what_is_next(self) -> None:
        self._register()
        with contextlib.redirect_stdout(io.StringIO()):
            gate.main(["handoff", "--repo", str(self.repo), "--note", "remember y"])
        text = (self.repo / "HANDOFF.md").read_text()
        run = gate.run_info(self.repo)
        self.assertEqual(run["plan"], str(self.repo / ".handoff" / "plan.md"))
        self.assertEqual(run["divergence_log"], str(self.repo / ".handoff" / "divergence-log.md"))
        self.assertEqual((self.repo / ".handoff" / "request.md").read_text(), "build x")
        for part in ("Finish the plan gate: `/gate plan .handoff/plan.md`", "remember y", "/resume-handoff"):
            self.assertIn(part, text)
        exclude = (Path(gate.common_dir(self.repo)) / "info" / "exclude").read_text().splitlines()
        self.assertIn("HANDOFF.md", exclude)
        self.assertIn(".handoff/", exclude)
        self.assertEqual(git(self.repo, "status", "--porcelain").strip(), "")

    def test_it_follows_the_gates_after_every_command(self) -> None:
        self._register()
        with contextlib.redirect_stdout(io.StringIO()):
            gate.main(["handoff", "--repo", str(self.repo)])
        sha = gate.sha256_file(self.repo / ".handoff" / "plan.md")
        gate.save_json(gate.state_root(self.repo) / "plans" / f"{sha}.json", {"verdict": "APPROVED"})
        with contextlib.redirect_stdout(io.StringIO()):
            gate.main(["status", "--repo", str(self.repo)])
        self.assertIn("invoke the `implement-plan` skill", (self.repo / "HANDOFF.md").read_text())
        _, rdir = gate.next_round(gate.branch_dir(self.repo) / "review")
        finding = {"id": "C1", "severity": "blocker", "file": "a.py", "line": 1, "why": "w", "state": "open"}
        gate.save_json(rdir / "ingest.json", {"canary": {"result": "caught"}, "results": [
            {"id": "correctness", "status": "findings", "findings": [finding]}]})
        gate.save_json(gate.branch_dir(self.repo) / "attempts.json", {"review": 99})
        with contextlib.redirect_stdout(io.StringIO()):
            gate.main(["status", "--repo", str(self.repo)])
        self.assertIn("Stop and ask the user", (self.repo / "HANDOFF.md").read_text())

    def test_no_handoff_is_written_until_one_is_asked_for(self) -> None:
        self._register()
        with contextlib.redirect_stdout(io.StringIO()):
            gate.main(["status", "--repo", str(self.repo)])
        self.assertFalse((self.repo / "HANDOFF.md").exists())


class TestedTableTest(RepoCase):
    def test_rows_from_real_results(self) -> None:
        checks = {"tier": "standard", "checks": [
            {"id": "unit", "kind": "unit", "status": "pass", "reason": "r", "log": "u.log", "seconds": 3},
            {"id": "suite", "kind": "unit", "status": "fail", "reason": "r", "note": "base fails too",
             "log": "s.log", "seconds": 60},
            {"id": "smoke", "kind": "real", "status": "skipped", "reason": "max_usd = 0, real runs off (unverified)"}]}
        review = {"tier": "standard", "verdict": "PASS", "selected": [{"id": "lean"}], "open_nits": [],
                  "rounds": [{"round": "round-1", "canary": {"result": "caught"}, "seconds": 40, "blocking_found": 1}]}
        text = gate.tested_summary(self.cfg(), "b" * 40, checks, review, {"kind": "skip-review", "at": "t"})
        lines = text.splitlines()
        self.assertEqual(lines[0], "How it was tested (tree bbbbbbbbbb, gate time 103s):")
        self.assertEqual(lines[1], "- unit: unit PASS 3s, suite FAIL [base fails too] 60s, log s.log")
        self.assertEqual(lines[2], "- review: PASS, 1 reviewers, 1 rounds, 1 blocking found and resolved, 0 nits open")
        self.assertEqual(lines[3], "- missing: real (smoke: max_usd = 0, real runs off (unverified)); skip-review (skipped "
                                   "at your request); no check declared for mechanical, dry-run, ui")
        self.assertEqual(len(lines), 4)


class RejectTest(RepoCase):
    def test_reject_once_then_reraised_stays_open(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    timeout = 37\n    return x\n")
        tree = gate.index_tree(self.repo)
        stage_dir = gate.branch_dir(self.repo) / "review"
        block = ('```json\n{"reviewer": "lean", "tree": "%s", "checked": ["src/pkg/mod.py"], "findings": '
                 '[{"id": "LN2", "severity": "should-fix", "file": "src/pkg/mod.py", "line": 2, '
                 '"quote": "timeout = 37"}]}\n```' % tree)
        for n in (1, 2):
            _, rdir = gate.next_round(stage_dir)
            (rdir / "lean.out.md").write_text(block)
            gate.save_json(rdir / "dispatch.json", {"tree": tree, "dispatch": [
                {"id": "lean", "out": str(rdir / "lean.out.md"), "files": ["src/pkg/mod.py"]}]})
            report = gate.ingest_round(self.repo, rdir, stage_dir, self.repo, tree)
            state = report["results"][0]["findings"][0]["state"]
            if n == 1:
                self.assertEqual(state, "open")
                args = gate.build_parser().parse_args(["reject", "--repo", str(self.repo), "--finding", "lean#0",
                                                       "--cite", "src/pkg/mod.py:2", "--reason", "it is fine"])
                self.assertEqual(gate.cmd_reject(args), gate.EXIT_OK)
                self.assertEqual(gate.load_json(stage_dir / "state.json", {})["lean"]["clean_tree"], tree)
            else:
                self.assertEqual(state, "reraised")
                args = gate.build_parser().parse_args(["reject", "--repo", str(self.repo), "--finding", "lean#0",
                                                       "--cite", "src/pkg/mod.py:2", "--reason", "again"])
                with self.assertRaises(gate.GateError):
                    gate.cmd_reject(args)


class RejectCanaryTest(RepoCase):
    def test_reject_in_a_missed_canary_round_leaves_the_reviewer_unclean(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    timeout = 37\n    return x\n")
        tree = gate.index_tree(self.repo)
        stage_dir = gate.branch_dir(self.repo) / "review"
        _, rdir = gate.next_round(stage_dir)
        (rdir / "lean.out.md").write_text(
            '```json\n{"reviewer": "lean", "tree": "%s", "checked": ["src/pkg/mod.py"], "findings": '
            '[{"id": "LN2", "severity": "should-fix", "file": "src/pkg/mod.py", "line": 2, '
            '"quote": "timeout = 37"}]}\n```' % tree)
        fx = Path(self.tmp.name) / "fx"
        write(fx, "files/a.py", "TIMEOUT = 37\n")
        write(fx, "expected.json", json.dumps({"id": "MV1", "file": "a.py", "line": 1}))
        (rdir / "canary.out.md").write_text('```json\n{"reviewer": "x", "tree": "t", "checked": ["a.py"], "findings": []}\n```')
        gate.save_json(rdir / "dispatch.json", {"tree": tree, "dispatch": [
            {"id": "lean", "out": str(rdir / "lean.out.md"), "files": ["src/pkg/mod.py"]}],
            "canary": {"id": "magic-values", "out": str(rdir / "canary.out.md"), "fixture": str(fx)}})
        gate.ingest_round(self.repo, rdir, stage_dir, self.repo, tree)
        args = gate.build_parser().parse_args(["reject", "--repo", str(self.repo), "--finding", "lean#0",
                                               "--cite", "src/pkg/mod.py:2", "--reason", "it is fine"])
        self.assertEqual(gate.cmd_reject(args), gate.EXIT_OK)
        self.assertIsNone(gate.load_json(stage_dir / "state.json", {})["lean"]["clean_tree"])


class CaptureTest(RepoCase):
    def test_capture_matches_by_prompt_and_copies_verbatim(self) -> None:
        rdir = Path(self.tmp.name) / "run"
        tasks = Path(self.tmp.name) / "tasks"
        tasks.mkdir()
        rdir.mkdir()
        prompt = str(rdir / "lean.prompt.md")
        gate.save_json(rdir / "dispatch.json", {"dispatch": [{"id": "lean", "name": "lean", "prompt": prompt,
                                                              "out": str(rdir / "lean.out.md")}]})
        report = 'notes\n```json\n{"reviewer": "lean", "tree": "t", "checked": [], "findings": []}\n```'
        lines = [{"type": "user", "timestamp": "2026-10-25T14:00:00.000Z",
                  "message": {"content": f"Read `{prompt}` in full and follow it exactly."}},
                 {"type": "assistant", "timestamp": "2026-10-25T14:00:12.500Z", "message": {"content": [
                     {"type": "tool_use", "name": "SubagentHandback", "input": {"message": report}}]}}]
        (tasks / "x.output").write_text("\n".join(json.dumps(l) for l in lines))
        (tasks / "other.output").write_text(json.dumps({"type": "user", "message": {"content": "unrelated"}}))
        args = gate.build_parser().parse_args(["capture", "--dir", str(rdir), "--transcripts", str(tasks)])
        self.assertEqual(gate.cmd_capture(args), gate.EXIT_OK)
        self.assertEqual((rdir / "lean.out.md").read_text(), report)
        self.assertEqual(gate.load_json(rdir / "durations.json", {})["lean"], 12.5)

    def test_a_short_handback_after_the_full_report_keeps_the_report(self) -> None:
        path = Path(self.tmp.name) / "t.output"
        report = 'full\n```json\n{"reviewer": "tests", "tree": "t", "checked": [], "findings": []}\n```'
        lines = [{"type": "user", "message": {"content": "Read `/r/tests.prompt.md` in full."}},
                 {"type": "assistant", "message": {"content": [{"type": "text", "text": report}]}},
                 {"type": "assistant", "message": {"content": [
                     {"type": "tool_use", "name": "SubagentHandback", "input": {"message": "short, no block"}}]}}]
        path.write_text("\n".join(json.dumps(l) for l in lines))
        self.assertEqual(gate.transcript_report(path)[1], report)
        lines[1]["message"]["content"][0]["text"] = "no block either"
        path.write_text("\n".join(json.dumps(l) for l in lines))
        self.assertEqual(gate.transcript_report(path)[1], "short, no block")   # none has one: the last message

    def test_prompt_pointer_without_backticks(self) -> None:
        for text in ("Read `/r/lean.prompt.md` in full.", "Read /r/lean.prompt.md and follow it exactly."):
            self.assertEqual(gate.PROMPT_POINTER.search(text).group(1), "/r/lean.prompt.md", text)


class SummaryTest(RepoCase):
    def test_summary_lists_reviewers_checks_and_leftovers(self) -> None:
        self.stage("src/pkg/mod.py", "def f(x):\n    return x * 3\n")
        tdir = gate.tree_dir(self.repo, gate.index_tree(self.repo))
        gate.save_json(tdir / "review.json", {"verdict": "PASS", "tier": "standard", "tier_reason": "r", "rounds": [],
                                              "selected": [{"id": "lean", "reason": "@code matched", "model": "standard"}],
                                              "skipped": [{"id": "docs-drift", "reason": "no file matched"}],
                                              "open_nits": [], "open_blocking": []})
        gate.save_json(tdir / "checks.json", {"verdict": "PASS", "tier": "standard", "checks": [
            {"id": "unit", "kind": "unit", "reason": "@code matched", "status": "pass", "seconds": 1.0}]})
        gate.save_json(tdir / "audit-staged.json", {"leftovers": ["notes.txt"]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gate.cmd_summary(gate.build_parser().parse_args(["summary", "--repo", str(self.repo)]))
        text = out.getvalue()
        self.assertIn("| lean | @code matched | standard |", text)
        self.assertIn("Skipped: docs-drift (no file matched)", text)
        self.assertIn("| unit | unit | @code matched | pass |", text)
        self.assertIn("Leftovers (untracked, not committed; clean up): notes.txt", text)

    def test_the_divergences_stay_out_of_the_session_and_the_mr(self) -> None:
        def summary(*flags: str) -> str:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                gate.cmd_summary(gate.build_parser().parse_args(["summary", "--repo", str(self.repo), *flags]))
            return out.getvalue()

        log = Path(self.tmp.name) / "log.md"
        log.write_text("| # | tag | plan said | did instead | why |\n|---|---|---|---|---|\n| 1 | [MECH] | p | d | w |\n")
        gate.save_json(gate.branch_dir(self.repo) / "run.json", {"divergence_log": str(log)})
        self.assertIn("How it was tested", summary("--tested"))
        self.assertNotIn("Divergences", summary("--tested"))
        self.assertNotIn("Divergences", summary())

    def test_a_trimmed_cell_closes_the_code_span_it_cut(self) -> None:
        import gate_view

        self.assertEqual(gate_view.trim_cell("short", 12), "short")
        self.assertEqual(gate_view.trim_cell("see `a very long path name`", 12), "see `a very` …")

    def test_nothing_ran_says_what_is_missing_and_lists_no_kinds(self) -> None:
        text = gate.tested_summary(self.cfg(), "a" * 40, None, None, None)
        self.assertEqual(text.splitlines()[1:], ["- missing: checks (no checks stamp); review (no review stamp)"])


if __name__ == "__main__":
    unittest.main()
