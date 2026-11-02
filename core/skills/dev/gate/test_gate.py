"""Tests for gate.py: tiers, tighten-only merge, quote checks, stamps, hygiene, summary.

Run: uv run --no-project --python 3.12 python -m unittest discover -s <this folder> -p 'test_*.py'
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
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

    def answer(self, dispatch: dict, findings: dict | None = None, invalid: tuple = ()) -> dict:
        """Every dispatched reviewer reports what it was assigned; `findings` maps a reviewer to its findings."""
        for e in dispatch["dispatch"]:
            body = {"reviewer": e["id"], "tree": dispatch["tree"], "checked": e["files"],
                    "findings": (findings or {}).get(e["id"], [])}
            Path(e["out"]).write_text("no block" if e["id"] in invalid else f"```json\n{json.dumps(body)}\n```")
        rdir = Path(dispatch["dispatch"][0]["out"]).parent
        return gate.ingest_round(self.repo, rdir, self.stage_dir, self.repo, dispatch["tree"])

    @staticmethod
    def finding(line: int, severity: str = "should-fix", rid: str = "LN1", path: str = "src/pkg/big.py",
                quote: str | None = None) -> dict:
        return {"id": rid, "severity": severity, "file": path, "line": line, "quote": quote or f"V{line} = {line}",
                "why": "w", "fix": "f", "plain": "p"}

    def edit_big(self, line: int) -> None:
        """Change one more line of big.py; earlier edits stay."""
        text = (self.repo / "src/pkg/big.py").read_text()
        self.stage("src/pkg/big.py", text.replace(f"V{line} = {line}\n", f"V{line} = {line * 100}\n"))

    def result(self, report: dict, rid: str) -> dict:
        return next(r for r in report["results"] if r["id"] == rid)

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
        self.assertIn(first["tree"], prompt)
        self.assertIn("quote: `V2 = 2`", prompt)
        patch = Path(prompt.split("The diff under review: `", 1)[1].split("`", 1)[0]).read_text()
        self.assertIn("+W = 2", patch)
        self.assertNotIn("V5", patch)                                                # reviewed already
        fidelity = next(e for e in second["dispatch"] if e["id"] == "fidelity")
        self.assertEqual(fidelity["files"], ["src/pkg/other.py"])

    def test_a_test_only_change_reruns_only_the_tests_reviewer(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare())                                                  # logic
        self.answer(self.prepare())                                                  # then quality
        self.stage("tests/test_mod.py", "def test_f():\n    assert True\n")
        self.assertEqual([e["id"] for e in self.prepare()["dispatch"]], ["tests"])

    def test_a_new_should_fix_off_the_changed_lines_is_kept_as_a_nit(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare())
        self.edit_big(12)
        report = self.answer(self.prepare(), {"correctness": [self.finding(3), self.finding(12, quote="V12 = 1200"),
                                                       self.finding(18, severity="blocker")]})
        correctness = self.result(report, "correctness")
        self.assertEqual([f["severity"] for f in correctness["findings"]], ["nit", "should-fix", "blocker"])
        self.assertIn("unchanged since", correctness["findings"][0]["late"])
        self.assertEqual(correctness["status"], "findings")
        self.edit_big(14)
        report = self.answer(self.prepare(), {"correctness": [self.finding(3)]})
        self.assertEqual(self.result(report, "correctness")["status"], "clean")             # only a late one: passes

    def test_an_unfixed_earlier_finding_still_blocks(self) -> None:
        self.edit_big(5)
        self.answer(self.prepare(), {"correctness": [self.finding(2)]})
        self.stage("src/pkg/other.py", "W = 2\n")
        report = self.answer(self.prepare(), {"correctness": [self.finding(2)]})
        correctness = self.result(report, "correctness")
        self.assertEqual((correctness["status"], correctness["findings"][0]["severity"]), ("findings", "should-fix"))

    def test_an_invalid_report_keeps_the_last_valid_base(self) -> None:
        self.edit_big(5)
        first = self.prepare()
        self.answer(first)
        self.edit_big(12)
        self.answer(self.prepare(), invalid=("correctness",))
        self.edit_big(14)
        correctness = next(e for e in self.prepare()["dispatch"] if e["id"] == "correctness")
        self.assertEqual(correctness["base"], first["tree"])


    def test_quality_waits_until_logic_is_clean(self) -> None:
        self.edit_big(5)
        first = self.prepare()
        self.assertEqual(sorted(e["id"] for e in first["dispatch"]), ["correctness", "fidelity"])
        self.assertEqual(first["deferred"]["ids"], ["polish", "docs-drift", "tests"])
        self.assertNotIn("canary", first)                                            # proven by calibrate only
        self.answer(first, {"correctness": [self.finding(5, quote="V5 = 500")]})
        self.assertEqual([e["id"] for e in self.prepare()["dispatch"]], ["correctness"])   # still logic only
        self.edit_big(6)
        self.answer(self.prepare())
        third = self.prepare()
        self.assertEqual([e["id"] for e in third["dispatch"]], ["polish", "docs-drift", "tests"])
        self.assertNotIn("deferred", third)

    def test_excluded_paths_never_reach_the_reviewer(self) -> None:
        self.edit_big(5)
        self.stage("tests/test_mod.py", "def test_f():\n    assert True\n")
        self.answer(self.prepare())
        quality = {e["id"]: e["files"] for e in self.prepare()["dispatch"]}
        self.assertEqual(quality["polish"], ["src/pkg/big.py"])
        self.assertEqual(quality["tests"], ["src/pkg/big.py", "tests/test_mod.py"])


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
        self.assertNotIn("Divergence", text)
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
        self.assertIn("Nothing for you to decide yet", gate_view.render(gate_view.summary(bdir, {"review": 3})))
        with contextlib.redirect_stdout(io.StringIO()):
            gate.main(["note", "--repo", str(self.repo), "--finding", "correctness#0", "--done", "Fixed: no freeze."])
        text = gate_view.render(gate_view.summary(bdir, {"review": 1}))
        for part in ("You need to decide", "The page freezes.", "What the agent did:</b> Fixed: no freeze.",
                     "#accept-gate", "about 10 minutes", "all 1 of them", "2 &middot; Fixes made to the code", "Waiting for a reviewer to confirm",
                     "<span class='badge nice'>Nice to have</span>A name is odd.", "1 important"):
            self.assertIn(part, text)
        with self.assertRaises(gate.GateError):
            gate.cmd_note(gate.build_parser().parse_args(["note", "--repo", str(self.repo), "--finding", "correctness#5"]))

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
        table = gate.tested_table(self.cfg(), "b" * 40, checks, review, {"kind": "skip-review", "at": "t"})
        self.assertIn("gate time 103s", table)
        self.assertIn("suite: FAIL [base fails too]", table)
        self.assertIn("| real | — | n/a (smoke: max_usd = 0", table)
        self.assertIn("| review | 1 reviewers, 1 rounds, canary round-1: caught, 40s |", table)
        self.assertIn("| user skip | skip-review", table)
        self.assertIn("| not verified | smoke;", table)


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

    def test_every_kind_listed_when_nothing_ran(self) -> None:
        table = gate.tested_table(self.cfg(), "a" * 40, None, None, None)
        for kind in self.cfg()["report_kinds"]:
            self.assertIn(f"| {kind} |", table)
        self.assertIn("| not verified |", table)


if __name__ == "__main__":
    unittest.main()
