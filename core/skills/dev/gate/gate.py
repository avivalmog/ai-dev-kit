#!/usr/bin/env python3
"""Gate engine: picks reviewers and checks from the diff, verifies findings, writes the stamps the hook reads.

Shared by every project and every coding agent. The flow it serves is core/rules/DEV_FLOW.md in the kit; the
steps an agent follows are in SKILL.md next to this file. Run it as:

    uv run --no-project --python 3.12 <this folder>/gate.py <command> --repo <worktree>

Everything here is deterministic: the diff, the two TOML files and the reviewers' JSON blocks decide the
outcome, never the calling agent's judgment.
"""
from __future__ import annotations

import argparse
import ast
import copy
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import gate_view
from gate_common import (AUDIT_BRANCH, AUDIT_STAGED, BLOCKING, CANARY_OK, PASSING, PUSH_STAGES, SKIP_WAIVES, STAGE_CHECKS, STAGE_PLAN,
                         STAGE_PUSH, STAGE_REVIEW, STATE_DIR, STATUS_FAIL, STATUS_PASS, STATUS_SKIPPED, TOKEN_ACCEPT, TOKEN_APPROVE_PLAN,
                         TOKEN_TTL_S, TOKENS_DIR, TREE_HEAD, TREE_INDEX, TREE_SHORT_LEN, USER_SKIP_FILE, VERDICT_ACCEPTED,
                         VERDICT_APPROVED, VERDICT_PASS, repo_state, tree_state)

SKILL_DIR = Path(__file__).resolve().parent
DEFAULT_GATES = SKILL_DIR / "default-gates.toml"
DISPATCH_TEMPLATE = SKILL_DIR / "dispatch-prompt.md"
RULES_TEMPLATE = SKILL_DIR / "rules-reviewer-prompt.md"
OUTPUT_CONTRACT = SKILL_DIR / "output-contract.md"
REREVIEW_NOTE = SKILL_DIR / "rereview-note.md"   # appended to a delta re-review's prompt; calibration fixtures are full reviews
SHARED_CALIBRATION = SKILL_DIR / "calibration"
CORE_DIR = Path(os.environ["AI_KIT_HOME"]) / "core" if os.environ.get("AI_KIT_HOME") else SKILL_DIR.parents[2]
REVIEWERS_DIR = CORE_DIR / "reviewers"       # shared reviewers, one <id>.md each
GATE_STATE = STATE_DIR
CALIBRATION_RUNS = GATE_STATE / "calibration"
PROJECT_GATES = "reviewers/gates.toml"
PROJECT_CALIBRATION = "reviewers/calibration"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

QUOTE_WINDOW = 3            # lines either side of a finding's line where its quote may sit
PRUNE_DAYS = 30
CHECK_TIMEOUT_S = 60 * 60
MODEL_TIERS = ("light", "standard", "strong")   # weakest first
RE_REVIEW_MODEL = "standard"                    # a re-check reads a small change; the strongest model is for the first review
DEFAULT_MODEL = "standard"   # a model tier (light | standard | strong); each agent maps it to one of its models
TIER_LITE, TIER_STANDARD, TIER_FULL = "lite", "standard", "full"    # also the [tiers.*] table names
DEFAULT_REVIEWER_TIERS = (TIER_STANDARD, TIER_FULL)
ALL_TIERS = (TIER_LITE, TIER_STANDARD, TIER_FULL)
TIER_RANK = {t: i for i, t in enumerate(ALL_TIERS)}
LATE_SEVERITY = "should-fix"     # on a re-review, this severity blocks only on lines changed since the last round
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
REVIEWER_ATTEMPTS = "by_reviewer"          # attempts.json: each reviewer's failed rounds; a review's cap counts the worst
TRIPWIRE_DIR, ADHOC_DIR = "tripwire", "adhoc"   # review rounds that never count against the gated review
RUN_DIR_TREE_LEN = 12                      # tree id prefix naming a checks run dir
SEVERITIES = ("blocker", "should-fix", "nit")
KIND_ORDER = ("mechanical", "unit", "dry-run", "ui", "real")
STAGE_LOGIC = "logic"
STAGES = (STAGE_PLAN, STAGE_LOGIC, "quality")      # a reviewer's stage
REVIEW_STAGES = (STAGE_LOGIC, "quality")
RULES_KINDS = ("rules", "checklist", "adhoc")     # reviewers built from the shared rules template
CAP_KEYS = {STAGE_PLAN: "plan_rounds", STAGE_REVIEW: "review_rounds", STAGE_CHECKS: "check_rounds",
            STAGE_PUSH: "push_rounds"}

EXIT_OK, EXIT_FAIL, EXIT_ERROR, EXIT_CAP = 0, 1, 2, 3
HANDOFF_FILE, HANDOFF_DIR = "HANDOFF.md", ".handoff"
HANDOFF_COPIES = {"request": "request.md", "plan": "plan.md", "divergence_log": "divergence-log.md"}
"""Run file -> its copy's name in HANDOFF_DIR."""
EXIT_TIMEOUT = 124          # what `timeout(1)` returns; recorded for a check that ran past CHECK_TIMEOUT_S


class GateError(Exception):
    """A config, git or state problem the caller must fix; printed, then the process exits with `code`."""

    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- globs

@functools.lru_cache(maxsize=None)
def glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a repo-root-anchored glob: `**` spans directories, `*` and `?` stay inside one segment."""
    p = pattern.lstrip("/")
    parts: list[str] = []
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif p.startswith("**", i):
            parts.append(".*")
            i += 2
        elif p[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif p[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(p[i]))
            i += 1
    return re.compile("".join(parts) + r"\Z")


def expand_globs(patterns: Iterable[str], globs: dict[str, list[str]]) -> list[str]:
    """Replace each `@name` with the patterns listed under `[globs] name`."""
    out: list[str] = []
    for pat in patterns:
        if pat.startswith("@"):
            if pat[1:] not in globs:
                raise GateError(f"unknown glob alias {pat}")
            out.extend(globs[pat[1:]])
        else:
            out.append(pat)
    return out


def first_match(path: str, patterns: Iterable[str]) -> str | None:
    """Return the first pattern matching `path`, or None."""
    for pat in patterns:
        if glob_regex(pat).match(path):
            return pat
    return None


# --------------------------------------------------------------------------- config

PROJECT_KEYS = {"version", "base", "max_usd", "doc_maps", "caps", "commit", "push", "baseline", "tiers",
                "tests", "reviewer", "checks", "globs"}
APPEND = {"tiers.full.always_when", "push.forbid_paths", "push.secret_patterns",
          "commit.junk_patterns", "commit.scratch_markers", "globs.data"}
LOWER_ONLY = {"tiers.lite.max_changed_lines", "tiers.standard.max_changed_lines", "commit.max_file_kb"}
NARROW_ONLY = {"tiers.lite.paths", "push.template_suffixes"}
FALSE_ONLY = {"tiers.lite.python_ast_neutral"}
FREE = {"baseline.link", "baseline.env"}


def _append(old: list[Any], new: list[Any]) -> list[Any]:
    """Old items first, then the new ones not already present."""
    return list(old) + [item for item in new if item not in old]


def _apply_rule(dotted: str, old: Any, new: Any) -> Any:
    """Return the merged value for one project key, or refuse a value that loosens the default."""
    if dotted in APPEND:
        return _append(old or [], new)
    if dotted in LOWER_ONLY:
        if new > old:
            raise GateError(f"{PROJECT_GATES}: {dotted} = {new} loosens the shared {old}; it can only be lowered")
        return new
    if dotted in NARROW_ONLY:
        extra = [p for p in new if p not in old]
        if extra:
            raise GateError(f"{PROJECT_GATES}: {dotted} adds {extra}; it can only be narrowed")
        return new
    if dotted in FALSE_ONLY:
        if new and not old:
            raise GateError(f"{PROJECT_GATES}: {dotted} can only be switched off")
        return new
    if dotted in FREE:
        return new
    raise GateError(f"{PROJECT_GATES}: unknown or fixed key '{dotted}'")


def _merge_nested(cfg: dict[str, Any], key: str, value: Any, dotted: str) -> None:
    """Walk one project table onto the merged config under the tighten-only rules."""
    if isinstance(value, dict) and dotted not in FREE:
        target = cfg.setdefault(key, {})
        if not isinstance(target, dict):
            raise GateError(f"{PROJECT_GATES}: '{dotted}' is not a table in the shared default")
        for sub, subval in value.items():
            _merge_nested(target, sub, subval, f"{dotted}.{sub}")
        return
    cfg[key] = _apply_rule(dotted, cfg.get(key), value)


def merge_config(default: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """Merge a project's gates.toml onto the shared default; refuse anything that loosens a default."""
    cfg = copy.deepcopy(default)
    for key, value in project.items():
        if key == "shared":
            raise GateError(f"{PROJECT_GATES}: [[shared]] reviewers live only in default-gates.toml; "
                            "a project can't remove or narrow them")
        if key not in PROJECT_KEYS:
            raise GateError(f"{PROJECT_GATES}: unknown key '{key}'")
        if key in ("version", "base", "max_usd", "doc_maps", "tests", "reviewer", "checks"):
            cfg[key] = value       # a project's [[checks]] replace the shared fallback check
        elif key == "caps":
            for name, n in value.items():
                if name not in cfg["caps"] or not isinstance(n, int) or n < 1:
                    raise GateError(f"{PROJECT_GATES}: caps.{name} must be a known cap and an integer >= 1")
                cfg["caps"][name] = n
        else:
            _merge_nested(cfg, key, value, key)
    return cfg


def load_config(repo: Path) -> dict[str, Any]:
    """The shared default merged with the repo's reviewers/gates.toml, when it has one."""
    default = tomllib.loads(DEFAULT_GATES.read_text(encoding="utf-8"))
    project_file = repo / PROJECT_GATES
    if not project_file.exists():
        return default
    try:
        project = tomllib.loads(project_file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise GateError(f"{PROJECT_GATES}: {exc}") from exc
    return merge_config(default, project)


# --------------------------------------------------------------------------- git and state

def run_git(repo: Path, *args: str, check: bool = True) -> str:
    """Run git in `repo` and return stdout; raise GateError on failure when `check`."""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GateError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def git_ok(repo: Path, *args: str) -> bool:
    """True when the git command exits 0."""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True).returncode == 0


def read_blob(repo: Path, spec: str) -> str | None:
    """Content of `<tree-ish>:<path>`, or None when it does not exist."""
    proc = subprocess.run(["git", "-C", str(repo), "show", spec], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def repo_root(path: str | None) -> Path:
    """Top level of the git worktree containing `path` (default: cwd)."""
    return Path(run_git(Path(path or os.getcwd()), "rev-parse", "--show-toplevel").strip())


def common_dir(repo: Path) -> str:
    """The repo's common git dir, the same from every worktree."""
    return run_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()


def state_root(repo: Path) -> Path:
    """`<state dir>/repos/<repo-id>`: shared by every worktree of the repo, outside any checkout."""
    return repo_state(common_dir(repo), GATE_STATE)


def branch_name(repo: Path) -> str:
    """Current branch, or `detached`."""
    return run_git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() or "detached"


def branch_dir(repo: Path) -> Path:
    """State for the current branch: run registration, attempts, review rounds, check state."""
    return state_root(repo) / "branches" / branch_name(repo).replace("/", "__")


def tree_dir(repo: Path, tree: str) -> Path:
    """Stamps and logs for one tree."""
    return tree_state(common_dir(repo), tree, GATE_STATE)


def base_ref(repo: Path, cfg: dict[str, Any]) -> str:
    """`origin/<base>` when it exists, else the local base branch."""
    base = cfg.get("base", "main")
    return f"origin/{base}" if git_ok(repo, "rev-parse", "--verify", "--quiet", f"origin/{base}") else base


def index_tree(repo: Path) -> str:
    """Tree object of the index: what a plain `git commit` would record."""
    return run_git(repo, "write-tree").strip()


def head_tree(repo: Path) -> str:
    """Tree object of HEAD: what a push sends."""
    return run_git(repo, "rev-parse", "HEAD^{tree}").strip()


def load_json(path: Path, default: Any) -> Any:
    """Parsed JSON at `path`, or `default` when absent."""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path: Path, data: Any) -> None:
    """Write `data` as indented JSON, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def now_iso() -> str:
    """Local time, seconds precision."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def take_token(kind: str) -> dict[str, Any] | None:
    """Consume a fresh user-armed token (`accept-gate`, `approve-plan`, `skip-review`, `skip-gates`), or None."""
    path = TOKENS_DIR / f"{kind}.json"
    data = load_json(path, None)
    if not data:
        return None
    path.unlink()
    if time.time() - float(data.get("created", 0)) > TOKEN_TTL_S:
        return None
    return data


# --------------------------------------------------------------------------- diff, tier

@dataclass
class Target:
    """What a gate judges: files and lines changed between `start` and the tree `end`."""

    repo: Path
    start: str
    end: str
    files: list[str]
    lines: int


def numstat(repo: Path, *diff_args: str) -> tuple[list[str], int, list[str]]:
    """(files, added+removed lines, binary files) for a `git diff` range."""
    files: list[str] = []
    binary: list[str] = []
    lines = 0
    for row in run_git(repo, "diff", "--numstat", "--no-renames", *diff_args).splitlines():
        added, removed, path = row.split("\t", 2)
        files.append(path)
        if added == "-":
            binary.append(path)
        else:
            lines += int(added) + int(removed)
    return sorted(files), lines, sorted(binary)


def merge_base(repo: Path, cfg: dict[str, Any]) -> str:
    """Merge-base of HEAD and the base ref."""
    return run_git(repo, "merge-base", "HEAD", base_ref(repo, cfg)).strip()


def staged_target(repo: Path, cfg: dict[str, Any], strict: bool = True) -> Target:
    """The staged state against the merge-base; strict refuses unstaged edits on staged paths."""
    start = merge_base(repo, cfg)
    files, lines, _ = numstat(repo, "--cached", start)
    if strict and not files:
        raise GateError("nothing staged against the merge-base; `git add <paths>` first")
    unstaged = set(run_git(repo, "diff", "--name-only", "--no-renames").splitlines())
    overlap = sorted(set(files) & unstaged)
    if strict and overlap:
        raise GateError("unstaged edits on staged paths, `git add` them first: " + ", ".join(overlap))
    return Target(repo, start, index_tree(repo), files, lines)


def range_target(repo: Path, spec: str) -> Target:
    """A committed range `A..B`, for explaining a past change."""
    start, _, end = spec.partition("..")
    end = end or "HEAD"
    files, lines, _ = numstat(repo, start, end)
    return Target(repo, start, run_git(repo, "rev-parse", f"{end}^{{tree}}").strip(), files, lines)


class _StripDocsAndHints(ast.NodeTransformer):
    """Remove docstrings and type annotations so two sources compare by behaviour only."""

    def _drop_docstring(self, node: Any) -> None:
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]

    def visit_Module(self, node: ast.Module) -> ast.AST:
        self._drop_docstring(node)
        return self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        self._drop_docstring(node)
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign):
                stmt.keep_annotation = True      # a class-body annotation declares a field
        return self.generic_visit(node)

    def _drop_hints(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Drop the signature's hints, unless a framework reads them at runtime: a decorated function
        (FastAPI, click) or a parameter defaulting to a call (`typer.Option(...)`, `Depends(...)`)."""
        args = node.args
        if node.decorator_list or any(isinstance(d, ast.Call) for d in args.defaults + args.kw_defaults if d):
            for a in args.posonlyargs + args.args + args.kwonlyargs + [x for x in (args.vararg, args.kwarg) if x]:
                a.keep_annotation = True
            return
        node.returns = None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self._drop_docstring(node)
        self._drop_hints(node)
        return self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self._drop_docstring(node)
        self._drop_hints(node)
        return self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> ast.AST:
        if not getattr(node, "keep_annotation", False):
            node.annotation = None
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST | None:
        if getattr(node, "keep_annotation", False):
            return node
        if node.value is None:
            return None
        return ast.Assign(targets=[node.target], value=node.value)


def behaviour_dump(source: str) -> str:
    """AST dump of `source` without docstrings, annotations, comments or positions."""
    return ast.dump(_StripDocsAndHints().visit(ast.parse(source)), include_attributes=False)


def ast_neutral(repo: Path, start: str, end: str, path: str) -> bool:
    """True when a .py file changed only in comments, docstrings or type hints."""
    if not path.endswith(".py"):
        return False
    old, new = read_blob(repo, f"{start}:{path}"), read_blob(repo, f"{end}:{path}")
    if old is None or new is None:
        return False
    try:
        return behaviour_dump(old) == behaviour_dump(new)
    except SyntaxError:
        return False


def compute_tier(cfg: dict[str, Any], files: list[str], lines: int | None,
                 neutral: Callable[[str], bool] | None) -> tuple[str, str]:
    """(tier, reason) from the changed files and line count; `lines=None` means a plan's file table."""
    tiers, globs = cfg["tiers"], cfg["globs"]
    always = expand_globs(tiers[TIER_FULL].get("always_when", []), globs)
    for path in files:
        pat = first_match(path, always)
        if pat:
            return TIER_FULL, f"{path} matches always_when {pat}"
    std_max = tiers[TIER_STANDARD]["max_changed_lines"]
    if lines is not None and lines > std_max:
        return TIER_FULL, f"{lines} changed lines > {std_max}"
    lite = tiers[TIER_LITE]
    lite_paths = expand_globs(lite["paths"], globs)

    def is_lite(path: str) -> bool:
        if first_match(path, lite_paths):
            return True
        return bool(neutral and lite.get("python_ast_neutral") and neutral(path))

    within = lines is None or lines <= lite["max_changed_lines"]
    if files and within and all(is_lite(p) for p in files):
        return TIER_LITE, "docs, comments, docstrings or type hints only" + (f", {lines} lines" if lines is not None else "")
    return TIER_STANDARD, f"{lines} changed lines" if lines is not None else "the plan lists code files"


def branch_tier(repo: Path, cfg: dict[str, Any], target: Target) -> tuple[str, str]:
    """The diff's tier, or the plan gate's predicted tier when that one is higher."""
    tier, reason = compute_tier(cfg, target.files, target.lines,
                                lambda f: ast_neutral(repo, target.start, target.end, f))
    run = run_info(repo)
    planned = run.get("plan_tier")
    if planned in TIER_RANK and TIER_RANK[planned] > TIER_RANK[tier]:
        return planned, f"the plan gate predicted {planned} ({run.get('plan_tier_reason', '')}); the diff alone is {tier}"
    return tier, reason


# --------------------------------------------------------------------------- reviewers and checks

@dataclass
class Reviewer:
    """One reviewer from the merged config."""

    id: str
    stage: str
    kind: str                  # shared | agent | rules | checklist | adhoc
    origin: str                # shared | project | adhoc
    applies_to: list[str]
    tiers: tuple[str, ...]
    model: str
    model_by_tier: dict[str, str]
    source: list[Path]
    rerun_skip: list[str] = field(default_factory=list)   # once clean, changes only here do not re-run it
    exclude: list[str] = field(default_factory=list)      # never shown these files, e.g. tests its lens exempts

    def agent_name(self) -> str | None:
        """Named read-only agent an adapter may install for a shared reviewer; None for the rest."""
        return f"review-{self.id}" if self.origin == "shared" else None

    def model_for(self, tier: str) -> str:
        """Model to dispatch at `tier`."""
        return self.model_by_tier.get(tier, self.model)


@dataclass
class Check:
    """One check command from the merged config."""

    id: str
    kind: str
    when: list[str]
    tiers: tuple[str, ...]
    run: str
    last: bool

    def group(self) -> int:
        """Run order: kind order, `last` checks after everything else."""
        return len(KIND_ORDER) if self.last else KIND_ORDER.index(self.kind)


@dataclass
class Pick:
    """Selection result for one reviewer or check, with its recorded reason."""

    id: str
    selected: bool
    reason: str
    files: list[str] = field(default_factory=list)


def _reviewer(entry: dict[str, Any], origin: str, repo: Path) -> Reviewer:
    """Build a Reviewer from one [[shared]] or [[reviewer]] table."""
    rid, kind = entry["id"], entry.get("kind", "shared")
    if entry.get("stage") not in STAGES:
        raise GateError(f"reviewer {rid}: stage must be one of {STAGES}")
    if origin == "shared":
        source = [REVIEWERS_DIR / f"{rid}.md"]
    elif kind == "agent":
        source = [repo / entry["prompt"]]
    elif kind == "rules":
        source = [repo / p for p in entry["rules"]]
    elif kind == "checklist":
        source = [repo / entry["checklist"]]
    else:
        raise GateError(f"reviewer {rid}: kind must be agent, rules or checklist")
    return Reviewer(rid, entry["stage"], kind, origin, list(entry["applies_to"]),
                    tuple(entry.get("tiers", DEFAULT_REVIEWER_TIERS)), entry.get("model", DEFAULT_MODEL),
                    dict(entry.get("model_by_tier", {})), source, list(entry.get("rerun_skip", [])),
                    list(entry.get("exclude", [])))


def all_reviewers(cfg: dict[str, Any], repo: Path) -> list[Reviewer]:
    """Shared reviewers first, then the project's; ids must be unique."""
    out = [_reviewer(e, "shared", repo) for e in cfg.get("shared", [])]
    out += [_reviewer(e, "project", repo) for e in cfg.get("reviewer", [])]
    ids = [r.id for r in out]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise GateError(f"duplicate reviewer ids: {dupes}")
    return out


def adhoc_rules_reviewer(spec: str, repo: Path) -> Reviewer:
    """`rules:<doc>[,<doc>]` or `checklist:<file>` from `--only`: one ad-hoc reviewer over every changed file."""
    kind, _, docs = spec.partition(":")
    if kind not in ("rules", "checklist") or not docs:
        raise GateError("--only takes rules:<doc>[,<doc>] or checklist:<file>")
    paths = [(repo / d.strip()).resolve() for d in docs.split(",")]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise GateError(f"{kind} file not found: {missing}")
    return Reviewer(f"{kind}-" + "-".join(p.stem for p in paths), "quality", "adhoc" if kind == "rules" else "checklist",
                    "adhoc", ["**"], ALL_TIERS, DEFAULT_MODEL, {}, paths)


def all_checks(cfg: dict[str, Any]) -> list[Check]:
    """Checks from the merged config, validated."""
    out = []
    for e in cfg.get("checks", []):
        if e.get("kind") not in KIND_ORDER:
            raise GateError(f"check {e.get('id')}: kind must be one of {KIND_ORDER}")
        missing = [k for k in ("id", "when", "run") if not e.get(k)]
        if missing:
            raise GateError(f"check {e.get('id')}: needs {missing}")
        out.append(Check(e["id"], e["kind"], list(e["when"]), tuple(e.get("tiers", ALL_TIERS)), e["run"],
                         bool(e.get("last", False))))
    return out


def pick_one(item_id: str, patterns: list[str], tiers: tuple[str, ...], files: list[str], tier: str,
             globs: dict[str, list[str]], extra_skip: str | None = None) -> Pick:
    """Apply the selection rule: some file matches its patterns, and the tier allows it."""
    expanded = expand_globs(patterns, globs)
    scoped = [f for f in files if first_match(f, expanded)]
    if not scoped:
        return Pick(item_id, False, "no file matched")
    if tier not in tiers:
        return Pick(item_id, False, f"tier {tier}", scoped)
    if extra_skip:
        return Pick(item_id, False, extra_skip, scoped)
    return Pick(item_id, True, f"{first_match(scoped[0], expanded)} matched {scoped[0]}", scoped)


def pick_reviewers(cfg: dict[str, Any], reviewers: list[Reviewer], files: list[str], tier: str,
                   stages: tuple[str, ...], tripwire: bool = False) -> list[tuple[Reviewer, Pick]]:
    """Selection for every reviewer in `stages`; tripwire keeps only project reviewers of risky files."""
    globs = cfg["globs"]
    risky_patterns = expand_globs(cfg["tiers"][TIER_FULL].get("always_when", []), globs)
    risky = [f for f in files if first_match(f, risky_patterns)]
    out = []
    for r in reviewers:
        if r.stage not in stages:
            continue
        excluded = expand_globs(r.exclude, globs)
        mine = [f for f in files if not first_match(f, excluded)]
        if not tripwire:
            out.append((r, pick_one(r.id, r.applies_to, r.tiers, mine, tier, globs)))
            continue
        p = pick_one(r.id, r.applies_to, ALL_TIERS, [f for f in risky if f in mine], tier, globs)
        if r.origin != "project":
            p = Pick(r.id, False, "tripwire runs project reviewers only", p.files)
        elif p.selected:
            p.reason = f"tripwire: {p.reason}"
        out.append((r, p))
    return out


def pick_checks(cfg: dict[str, Any], files: list[str], tier: str) -> list[tuple[Check, Pick]]:
    """Selection for every check; a `real` check also needs max_usd > 0."""
    out = []
    for c in all_checks(cfg):
        extra = "max_usd = 0, real runs off (unverified)" if c.kind == "real" and cfg.get("max_usd", 0) <= 0 else None
        out.append((c, pick_one(c.id, c.when, c.tiers, files, tier, cfg["globs"], extra)))
    return sorted(out, key=lambda cp: cp[0].group())


def changed_between(repo: Path, old_tree: str, new_tree: str) -> list[str] | None:
    """Files that differ between two trees, or None when the old tree is gone."""
    if old_tree == new_tree:
        return []
    proc = subprocess.run(["git", "-C", str(repo), "diff-tree", "-r", "--name-only", "--no-renames",
                           old_tree, new_tree], capture_output=True, text=True)
    return proc.stdout.splitlines() if proc.returncode == 0 else None


def still_clean(repo: Path, clean_tree: str | None, current: str, patterns: list[str],
                exclude: list[str] | None = None) -> bool:
    """A reviewer or check stays clean while no file in its scope (minus `exclude`) changed since its clean tree."""
    if not clean_tree:
        return False
    changed = changed_between(repo, clean_tree, current)
    if changed is None:
        return False
    return not any(first_match(f, patterns) and not first_match(f, exclude or []) for f in changed)


def stays_clean(repo: Path, cfg: dict[str, Any], r: Reviewer, state: dict[str, Any], tree: str) -> bool:
    """A reviewer clean on an earlier tree needs no re-run while nothing in its scope changed, minus its rerun_skip.

    One with an older bug still open re-checks it on any new code: its silence is what clears the bug."""
    globs, entry = cfg["globs"], state.get(r.id, {})
    if entry.get("older") and entry.get("clean_tree") != tree:
        return False
    return still_clean(repo, entry.get("clean_tree"), tree, expand_globs(r.applies_to, globs),
                       expand_globs(cfg.get("exclude_paths", []) + r.rerun_skip + r.exclude, globs))


def changed_lines(repo: Path, old_tree: str, new_tree: str, path: str) -> list[tuple[int, int]]:
    """(first, last) new-side line ranges of `path` that differ between two trees; a deletion is a one-line range."""
    text = run_git(repo, "diff", "-U0", "--no-renames", old_tree, new_tree, "--", path, check=False)
    return [(int(m.group(1)), int(m.group(1)) + max(int(m.group(2) or 1), 1) - 1) for m in HUNK.finditer(text)]


# --------------------------------------------------------------------------- calibration

def sha256_file(path: Path) -> str:
    """Hex sha256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def calibration_file(r: Reviewer, repo: Path) -> Path:
    """Where a reviewer's calibration record lives: never in a checkout, so every branch and worktree shares it."""
    return SHARED_CALIBRATION / "calibrated.json" if r.origin == "shared" else state_root(repo) / "calibrated.json"


def legacy_calibration_file(repo: Path) -> Path:
    """The in-repo record project reviewers used before it moved out of the checkout; read, never written."""
    return repo / PROJECT_CALIBRATION / "calibrated.json"


def calibrated_shas(r: Reviewer, repo: Path) -> set[str]:
    """Every reviewer text that passed its fixtures: the latest record's `sha` plus the `shas` it carries."""
    records = [load_json(calibration_file(r, repo), {}).get(r.id)]
    if r.origin == "project":
        records.append(load_json(legacy_calibration_file(repo), {}).get(r.id))
    return {sha for rec in records if rec for sha in (rec.get("sha"), *rec.get("shas", [])) if sha}


def fixture_dir(r: Reviewer, repo: Path) -> Path:
    """Directory holding a reviewer's caught/ and clean/ fixtures."""
    return (SHARED_CALIBRATION if r.origin == "shared" else repo / PROJECT_CALIBRATION) / r.id


def calibration_hash(r: Reviewer) -> str:
    """Hash of everything that shapes what the reviewer is sent; editing it forces recalibration.

    For the rules template only the part below its `---` line counts: the header above it documents the
    slots for maintainers and never reaches a reviewer.
    """
    if r.kind in RULES_KINDS:
        parts = [hashlib.sha256(template_body(RULES_TEMPLATE).encode()).hexdigest(), sha256_file(OUTPUT_CONTRACT)]
    else:
        parts = [sha256_file(p) for p in (r.source[0], DISPATCH_TEMPLATE, OUTPUT_CONTRACT)]
    return hashlib.sha256("".join(parts).encode()).hexdigest()


def missing_source(r: Reviewer) -> list[str]:
    """The reviewer's own files that do not exist; such a reviewer cannot run at all."""
    return [] if r.origin == "adhoc" else [str(p) for p in r.source if not p.exists()]


def calibration_problem(r: Reviewer, repo: Path) -> str | None:
    """Why a reviewer's calibration is not current (a warning, not a refusal), or None."""
    if r.origin == "adhoc":
        return None
    missing = missing_source(r)
    if missing:
        return f"source missing: {missing}"
    passed = calibrated_shas(r, repo)
    if not passed:
        return "never calibrated"
    if calibration_hash(r) not in passed:
        return "edited since its last calibration"
    model = (load_json(calibration_file(r, repo), {}).get(r.id) or {}).get("model")
    if model and model != r.model:
        return f"model changed since its last calibration ({model} -> {r.model})"
    return None


# --------------------------------------------------------------------------- prompts

def template_body(path: Path) -> str:
    """Text after the first `---` line of a template (the part sent to the reviewer)."""
    text = path.read_text(encoding="utf-8")
    head, sep, body = text.partition("\n---\n")
    return body if sep else text


def fill(template: str, values: dict[str, str]) -> str:
    """Replace every {{SLOT}}; refuse to send a prompt with an unfilled slot."""
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", value)
    leftover = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", out)))
    if leftover:
        raise GateError(f"prompt has unfilled slots: {leftover}")
    return out


def agent_body(path: Path) -> str:
    """An agent file without its YAML frontmatter."""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        _, _, rest = text[4:].partition("\n---\n")
        return rest
    return text


def build_prompts(r: Reviewer, ctx: dict[str, str]) -> tuple[str, str | None]:
    """(prompt, fallback prompt) for one dispatch; the fallback inlines a shared agent's body.

    A delta re-review (ctx `RE_REVIEW`) gets the re-review note appended after the calibrated part."""
    ctx = dict(ctx)
    note = ctx.pop("RE_REVIEW", "")
    if note:
        prompt, fallback = build_prompts(r, ctx)
        return prompt + "\n\n" + note, (fallback + "\n\n" + note) if fallback else None
    if r.kind in RULES_KINDS:
        values = dict(ctx)
        values["RULES_DOCS"] = "NONE" if r.kind == "checklist" else ", ".join(str(p) for p in r.source)
        if ctx.get("FROZEN_ALLOWLIST", "NONE") != "NONE":
            values["CHECKLIST_FILE"] = ctx["FROZEN_ALLOWLIST"]
        elif r.kind == "checklist":
            values["CHECKLIST_FILE"] = str(r.source[0])
        else:
            values["CHECKLIST_FILE"] = "NONE"
        values.pop("FROZEN_ALLOWLIST", None)
        return fill(template_body(RULES_TEMPLATE), values), None
    values = {k: v for k, v in ctx.items() if k != "FROZEN_ALLOWLIST"}
    dispatch = fill(DISPATCH_TEMPLATE.read_text(encoding="utf-8"), values)
    if r.origin == "shared":
        return dispatch, agent_body(r.source[0]) + "\n\n" + dispatch
    return agent_body(r.source[0]) + "\n\n" + dispatch, None


def fake_tree(seed: str) -> str:
    """A sha-shaped id for a fixture, so a canary looks like any other review."""
    return hashlib.sha1(seed.encode()).hexdigest()


def stage_fixture(fx: Path, dest: Path, r: Reviewer) -> tuple[Path, str]:
    """Copy a fixture (minus its answer key) to a neutral workspace; return it and a neutral dispatch name."""
    tag = hashlib.sha1(f"{fx}|{dest}".encode()).hexdigest()[:6]
    ws = dest / f"ws-{tag}"
    shutil.copytree(fx, ws, ignore=shutil.ignore_patterns("expected.json"), dirs_exist_ok=True)
    return ws, f"{r.id}-{tag}"


def fixture_ctx(r: Reviewer, fixture: Path, round_label: str) -> dict[str, str]:
    """Prompt slots for running a reviewer on a calibration fixture."""
    root = fixture / "files"
    files = sorted(str(p) for p in root.rglob("*") if p.is_file())

    def opt(name: str) -> str:
        return str(fixture / name) if (fixture / name).exists() else "NONE"

    return {"REVIEWER_ID": r.id, "STAGE": r.stage, "ROUND": round_label, "TREE": fake_tree(str(fixture)),
            "REPO_ROOT": str(root), "REQUEST": opt("request.md"), "PLAN": opt("plan.md"),
            "DIVERGENCE_LOG": opt("divergence-log.md"), "DIFF": opt("diff.patch"), "FILES": "\n".join(files),
            "CHANGED_FILES": "\n".join(files), "DOC_MAPS": opt("doc-map.md"), "CONTRACT": str(OUTPUT_CONTRACT),
            "FROZEN_ALLOWLIST": "NONE"}


# --------------------------------------------------------------------------- reviewer output

JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def parse_output(text: str) -> dict[str, Any]:
    """The last fenced JSON block of a reviewer's output, validated against the output contract."""
    blocks = JSON_BLOCK.findall(text)
    if not blocks:
        raise ValueError("no ```json block")
    data = json.loads(blocks[-1])
    if not isinstance(data.get("findings"), list) or not isinstance(data.get("checked"), list):
        raise ValueError("the JSON block needs `findings` and `checked` lists")
    for f in data["findings"]:
        missing = [k for k in ("id", "severity", "file", "line", "quote") if k not in f]
        if missing:
            raise ValueError(f"finding lacks {missing}")
        if f["severity"] not in SEVERITIES:
            raise ValueError(f"severity {f['severity']!r} is not one of {SEVERITIES}")
        if not isinstance(f["line"], int):
            raise ValueError("finding line must be an integer")
    return data


def norm_ws(text: str) -> str:
    """Collapse whitespace so a quote matches regardless of indentation and wrapping."""
    return " ".join(text.split())


def rel_to(path: str, root: Path) -> str:
    """Repo-relative form of a path a reviewer reported (absolute or relative)."""
    p = Path(path)
    if p.is_absolute():
        try:
            return str(p.resolve().relative_to(root.resolve()))
        except ValueError:
            return str(p)
    return path


def read_source(repo: Path | None, tree: str | None, root: Path, path: str) -> str | None:
    """Text a finding points at: from the reviewed tree when it is a repo file, else from disk."""
    rel = rel_to(path, root)
    if repo is not None and tree and not Path(rel).is_absolute():
        blob = read_blob(repo, f"{tree}:{rel}")
        if blob is not None:
            return blob
    disk = Path(rel) if Path(rel).is_absolute() else root / rel
    return disk.read_text(encoding="utf-8", errors="replace") if disk.is_file() else None


def quote_found(repo: Path | None, tree: str | None, root: Path, finding: dict[str, Any]) -> bool:
    """True when the finding's quote sits within QUOTE_WINDOW lines of its cited line."""
    text = read_source(repo, tree, root, str(finding["file"]))
    quote = norm_ws(str(finding["quote"]))
    if text is None or not quote:
        return False
    lines = text.splitlines()
    line = int(finding["line"])
    window = [norm_ws(x) for x in lines[max(0, line - 1 - QUOTE_WINDOW):min(len(lines), line + QUOTE_WINDOW)]]
    return any(quote in w for w in window) or quote in " ".join(window)


def finding_key(reviewer: str, f: dict[str, Any], root: Path) -> str:
    """Identity of a finding across rounds, for spotting a re-raised rejection."""
    return f"{reviewer}|{f['id']}|{rel_to(str(f['file']), root)}|{norm_ws(str(f['quote']))}"


def judge_output(text: str, reviewer: str, assigned: list[str], repo: Path | None, tree: str | None,
                 root: Path, rejections: dict[str, Any], expect_tree: str | None, allow_older: bool = True) -> dict[str, Any]:
    """Validate one reviewer output: schema, tree, coverage, quotes, re-raised rejections."""
    try:
        data = parse_output(text)
    except (ValueError, json.JSONDecodeError) as exc:
        return {"id": reviewer, "status": "invalid", "error": str(exc), "findings": []}
    if expect_tree and data.get("tree") != expect_tree:
        return {"id": reviewer, "status": "invalid", "error": f"{gate_view.WRONG_TREE_ERROR}{data.get('tree')!r} is not {expect_tree}",
                "findings": []}
    checked = {rel_to(str(c), root) for c in data["checked"]}
    missing = [f for f in assigned if f not in checked]
    findings = []
    for f in data["findings"]:
        item = dict(f)
        if not allow_older or item.get("older") is not True:   # only the code review asks the user about older bugs
            item.pop("older", None)
        item["key"] = finding_key(reviewer, f, root)
        if not quote_found(repo, tree, root, f):
            item["state"] = "dropped"
        elif item["key"] in rejections:
            item["state"] = "reraised"
        else:
            item["state"] = "open"
        findings.append(item)
    blocking = [f for f in findings if blocks(f)]
    if missing:
        return {"id": reviewer, "status": "invalid", "error": f"{gate_view.MISSED_FILES_ERROR} {missing}",
                "findings": findings}
    return {"id": reviewer, "status": "findings" if blocking else "clean", "error": "", "findings": findings}


def blocks(f: dict[str, Any]) -> bool:
    """A real defect still open that this change owns; an older bug waits for the user instead."""
    return f["state"] in ("open", "reraised") and f["severity"] in BLOCKING and not f.get("older")


def has_older_bug(findings: list[dict[str, Any]]) -> bool:
    """Whether a report still holds an older bug for the user to decide on."""
    return any(f.get("older") and f["state"] in ("open", "reraised") and f["severity"] in BLOCKING for f in findings)


def score_fixture(text: str, fixture: Path, reviewer: str, workspace: Path | None = None) -> dict[str, Any]:
    """Score an output on a fixture: caught/missed for a planted one, clean/noisy for a clean one."""
    expected = load_json(fixture / "expected.json", {})
    root = (workspace or fixture) / "files"
    files = expected.get("must_read", [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()])
    judged = judge_output(text, reviewer, files, None, None, root, {}, None)
    if judged["status"] == "invalid":
        return {"result": "invalid", "error": judged["error"], "false_positives": 0}
    reported = [f for f in judged["findings"] if f["state"] == "open"]   # a style lens rates its finds as nits
    hits = [f for f in reported if f["severity"] in BLOCKING]
    if expected.get("clean"):
        return {"result": "clean" if not hits else "noisy", "false_positives": len(hits), "error": ""}
    want_id = str(expected["id"]).lower()
    targets = expected.get("any_of") or [expected]     # a defect may be pointed at from more than one place

    def is_planted(f: dict[str, Any]) -> bool:
        id_ok = want_id == "*" or str(f["id"]).lower() == want_id
        rel = rel_to(str(f["file"]), root)
        return id_ok and any(rel == t["file"] and abs(int(f["line"]) - int(t["line"])) <= QUOTE_WINDOW
                             for t in targets)

    return {"result": "caught" if any(is_planted(f) for f in reported) else "missed",
            "false_positives": sum(1 for f in hits if not is_planted(f)), "error": ""}


# --------------------------------------------------------------------------- run registration

def run_info(repo: Path) -> dict[str, Any]:
    """The branch's registered request, plan and divergence log (from `init`)."""
    return load_json(branch_dir(repo) / "run.json", {})


def slot_path(value: str | None) -> str:
    """A path slot value, or NONE."""
    return value if value and Path(value).exists() else "NONE"


def doc_maps(cfg: dict[str, Any], repo: Path) -> str:
    """Absolute doc-map paths, or NONE."""
    paths = [str(repo / p) for p in cfg.get("doc_maps", []) if (repo / p).exists()]
    return "\n".join(paths) if paths else "NONE"


def check_cap(repo: Path, stage: str, cfg: dict[str, Any]) -> None:
    """Refuse to start another run of `stage` once its failed attempts reach the cap: stop and ask the user."""
    cap = int(cfg["caps"][CAP_KEYS[stage]])
    used = load_json(branch_dir(repo) / "attempts.json", {}).get(stage, 0)
    if used >= cap:
        raise GateError(f"{stage}: all {cap} attempts failed. Stop and ask the user: "
                        "#accept-gate, direction (then reset-attempts), or #abort", EXIT_CAP)


def count_failure(repo: Path, stage: str, reviewers: Iterable[str] = ()) -> int:
    """Record one failed attempt at `stage` (a run that passes costs nothing): one for each reviewer named, if any.

    The stage's count is then its worst reviewer's, so one reviewer's failures never use up another's tries."""
    path = branch_dir(repo) / "attempts.json"
    data = load_json(path, {})
    mine = data.setdefault(REVIEWER_ATTEMPTS, {}) if reviewers else {}
    for rid in reviewers:
        mine[rid] = mine.get(rid, 0) + 1
    data[stage] = max(data.get(stage, 0), *mine.values()) if mine else data.get(stage, 0) + 1
    save_json(path, data)
    return data[stage]


def reset_attempt(repo: Path, stage: str) -> None:
    """Give a stage a fresh set of attempts."""
    path = branch_dir(repo) / "attempts.json"
    data = load_json(path, {})
    data[stage] = 0
    if stage == STAGE_REVIEW:
        data.pop(REVIEWER_ATTEMPTS, None)
    save_json(path, data)


def next_round(stage_dir: Path) -> tuple[int, Path]:
    """Next round number and its directory."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    n = len([p for p in stage_dir.iterdir() if p.is_dir() and p.name.startswith("round-")]) + 1
    path = stage_dir / f"round-{n}"
    path.mkdir()
    return n, path


def write_dispatch(round_dir: Path, r: Reviewer, ctx: dict[str, str], model: str, name: str) -> dict[str, Any]:
    """Write one reviewer's prompt files and return its dispatch entry."""
    prompt, fallback = build_prompts(r, ctx)
    (round_dir / f"{name}.prompt.md").write_text(prompt, encoding="utf-8")
    entry = {"id": r.id, "name": name, "agent_name": r.agent_name(), "model_tier": model,
             "prompt": str(round_dir / f"{name}.prompt.md"), "out": str(round_dir / f"{name}.out.md")}
    if fallback:
        (round_dir / f"{name}.fallback.prompt.md").write_text(fallback, encoding="utf-8")
        entry["fallback_prompt"] = str(round_dir / f"{name}.fallback.prompt.md")
    return entry


# --------------------------------------------------------------------------- review commands

def review_ctx(repo: Path, cfg: dict[str, Any], r: Reviewer, files: list[str], tree: str, round_label: str,
               diff_path: Path, stage_dir: Path) -> dict[str, str]:
    """Prompt slots for a real review of the repo."""
    run = run_info(repo)
    absolute = "\n".join(str(repo / f) for f in files)
    frozen = stage_dir / f"allowlist-{r.id}.yaml"
    return {"REVIEWER_ID": r.id, "STAGE": r.stage, "ROUND": round_label, "TREE": tree, "REPO_ROOT": str(repo),
            "REQUEST": slot_path(run.get("request")), "PLAN": slot_path(run.get("plan")),
            "DIVERGENCE_LOG": slot_path(run.get("divergence_log")), "DIFF": str(diff_path), "FILES": absolute,
            "CHANGED_FILES": absolute, "DOC_MAPS": doc_maps(cfg, repo), "CONTRACT": str(OUTPUT_CONTRACT),
            "FROZEN_ALLOWLIST": str(frozen) if frozen.exists() else "NONE"}


def review_selection(repo: Path, cfg: dict[str, Any], target: Target, args: argparse.Namespace
                     ) -> tuple[str, str, list[tuple[Reviewer, Pick]]]:
    """(tier, reason, picks) for review-prepare and stamp."""
    tier, reason = branch_tier(repo, cfg, target)
    files = reviewable(cfg, target.files)
    if getattr(args, "only", None):
        r = adhoc_rules_reviewer(args.only, repo)
        r = next((c for c in all_reviewers(cfg, repo) if c.kind in RULES_KINDS
                  and sorted(p.resolve() for p in c.source) == sorted(r.source)), r)
        return tier, reason, [(r, pick_one(r.id, r.applies_to, r.tiers, files, tier, cfg["globs"]))]
    picks = pick_reviewers(cfg, all_reviewers(cfg, repo), files, tier, REVIEW_STAGES,
                           tripwire=getattr(args, "tripwire", False))
    return tier, reason, picks


def review_dir(repo: Path, args: argparse.Namespace) -> Path:
    """Where a review's rounds live. Only the gated review's failures count against its attempts."""
    if getattr(args, "only", None):
        return branch_dir(repo) / ADHOC_DIR       # the report-only /rules-review alias
    return branch_dir(repo) / (TRIPWIRE_DIR if args.tripwire else STAGE_REVIEW)


def reviewable(cfg: dict[str, Any], files: list[str]) -> list[str]:
    """Changed files minus `exclude_paths` (planted-defect fixtures), the set reviewers are shown."""
    excluded = expand_globs(cfg.get("exclude_paths", []), cfg["globs"])
    return [f for f in files if not first_match(f, excluded)]


def review_base(stage_dir: Path, state: dict[str, Any], rid: str) -> tuple[str | None, list[dict[str, Any]]]:
    """(tree of the reviewer's last valid review, its blocking findings still open there), or (None, []) for none."""
    entry = state.get(rid, {})
    name = entry.get("valid_round") or entry.get("round")
    report = load_json(stage_dir / name / "ingest.json", None) if name else None
    result = next((x for x in (report or {}).get("results", []) if x["id"] == rid), None)
    if report is None or result is None or result["status"] == "invalid" or report["canary"]["result"] not in CANARY_OK:
        return None, []
    prior = [f for f in result["findings"] if f["state"] in ("open", "reraised") and f["severity"] in BLOCKING]
    return report["tree"], prior


def dispatch_model(r: Reviewer, tier: str, stage_dir: Path, state: dict[str, Any], re_review: bool = False) -> str:
    """The model tier to dispatch `r` at: RE_REVIEW_MODEL at most on a re-review; one up when its last report was invalid
    because it did not read its files."""
    model = r.model_for(tier)
    if re_review and model == MODEL_TIERS[-1]:
        model = RE_REVIEW_MODEL
    name = state.get(r.id, {}).get("round")
    report = load_json(stage_dir / name / "ingest.json", None) if name else None
    last = next((x for x in (report or {}).get("results", []) if x["id"] == r.id), None)
    if model not in MODEL_TIERS or not last or last["status"] != "invalid" \
            or not last.get("error", "").startswith(gate_view.MISSED_FILES_ERROR):
        return model
    return MODEL_TIERS[min(MODEL_TIERS.index(model) + 1, len(MODEL_TIERS) - 1)]


def last_dispatched_model(r: Reviewer, tier: str, stage_dir: Path, state: dict[str, Any]) -> str:
    """The model tier `r` last ran at, from its round's dispatch; the configured one when it has not run."""
    name = state.get(r.id, {}).get("round")
    dispatch = load_json(stage_dir / name / "dispatch.json", None) if name else None
    entry = next((e for e in (dispatch or {}).get("dispatch", []) if e["id"] == r.id), None)
    return entry["model_tier"] if entry else r.model_for(tier)


def rereview_note(base: str, prior: list[dict[str, Any]]) -> str:
    """The re-review note: the tree it reviewed last, and the findings it must re-check."""
    items = [f"- {f['id']} `{f['file']}:{f['line']}`: {f.get('why', '')}\n  quote: `{f['quote']}`"
             + (" (older: true)" if f.get("older") else "") for f in prior]
    return (REREVIEW_NOTE.read_text(encoding="utf-8").replace("{{BASE_TREE}}", base)
            .replace("{{PRIOR_FINDINGS}}", "\n".join(items) or "None."))


def cmd_review_prepare(args: argparse.Namespace) -> int:
    """Select reviewers, skip the ones still clean, write prompts for one parallel round."""
    repo = repo_root(args.repo)
    cfg = load_config(repo)
    target = staged_target(repo, cfg)
    tier, reason, picks = review_selection(repo, cfg, target, args)
    selected = [(r, p) for r, p in picks if p.selected]
    missing = [f"{r.id}: {m}" for r, _ in selected if (m := missing_source(r))]
    if missing:
        raise GateError("reviewer files missing:\n  " + "\n  ".join(missing))
    problems = []
    for r, _ in selected:
        why = calibration_problem(r, repo)
        if why:
            problems.append(f"{r.id}: {why}")
    if problems:
        print("gate: warning, running uncalibrated reviewers (prove each once: /gate calibrate <ids>):\n  "
              + "\n  ".join(problems), file=sys.stderr)
    stage_dir = review_dir(repo, args)
    state = load_json(stage_dir / "state.json", {})
    dispatch = [(r, p) for r, p in selected if not stays_clean(repo, cfg, r, state, target.end)]
    dispatch_ids = {r.id for r, _ in dispatch}
    result: dict[str, Any] = {"tree": target.end, "tier": tier, "tier_reason": reason,
                              "skipped": [{"id": p.id, "reason": p.reason} for _, p in picks if not p.selected],
                              "clean": [r.id for r, _ in selected if r.id not in dispatch_ids], "dispatch": []}
    if not dispatch:
        result["note"] = "every selected reviewer is clean for this tree; run `stamp --stage review`"
        print(json.dumps(result, indent=2))
        return EXIT_OK
    if stage_dir.name == STAGE_REVIEW:
        check_cap(repo, STAGE_REVIEW, cfg)
    n, round_dir = next_round(stage_dir)
    diff_path = round_dir / "diff.patch"
    diff_path.write_text(run_git(repo, "diff", "--cached", "--no-renames", target.start), encoding="utf-8")
    for r, p in dispatch:
        files = [f for f in p.files if git_ok(repo, "cat-file", "-e", f"{target.end}:{f}")]   # deleted files: nothing to read
        ctx = review_ctx(repo, cfg, r, files, target.end, str(n), diff_path, stage_dir)
        extra: dict[str, Any] = {"files": files}
        base, prior = review_base(stage_dir, state, r.id)
        changed = changed_between(repo, base, target.end) if base else None
        if base and changed is not None:            # reviewed before: only what changed since, plus its open findings
            redo = [rel_to(str(f["file"]), repo) for f in prior]
            delta = sorted({f for f in files if f in changed}
                           | {f for f in redo if git_ok(repo, "cat-file", "-e", f"{target.end}:{f}")})
            if delta:
                patch = round_dir / f"{r.id}.delta.patch"
                patch.write_text(run_git(repo, "diff", "--no-renames", base, target.end, "--", *delta), encoding="utf-8")
                ctx = review_ctx(repo, cfg, r, delta, target.end, str(n), patch, stage_dir)
                ctx["RE_REVIEW"] = rereview_note(base, prior)
                extra = {"files": delta, "base": base,
                         "prior": [{k: f[k] for k in ("key", "id", "file", "line", "quote")} for f in prior]}
        result["dispatch"].append(write_dispatch(round_dir, r, ctx, dispatch_model(r, tier, stage_dir, state, "base" in extra), r.id) | extra)
    # No per-round canary: a reviewer is proven once, by `/gate calibrate`, after its own files change.
    result["round"] = n
    save_json(round_dir / "dispatch.json", result | {"prepared_at": time.time()})
    print(json.dumps(result, indent=2))
    return EXIT_OK


def latest_round(stage_dir: Path, requested: int | None) -> Path:
    """Directory of the requested round, or the newest."""
    rounds = sorted((p for p in stage_dir.glob("round-*") if p.is_dir()), key=lambda p: int(p.name.split("-")[1]))
    if not rounds:
        raise GateError(f"no round has been prepared in {stage_dir}")
    if requested is None:
        return rounds[-1]
    path = stage_dir / f"round-{requested}"
    if not path.is_dir():
        raise GateError(f"{path} does not exist")
    return path


def freeze_allowlist(text: str, path: Path) -> None:
    """Save a rules reviewer's echoed allowlist once, so later rounds check the same rule set."""
    if path.exists():
        return
    m = re.search(r"### Allowlist used\s*(.*?)\n### ", text, re.DOTALL)
    if m and m.group(1).strip():
        path.write_text(m.group(1).strip().strip("`").removeprefix("yaml").strip() + "\n", encoding="utf-8")


def same_finding(f: dict[str, Any], prior: dict[str, Any], root: Path) -> bool:
    """`f` re-reports `prior`: same key, or the same file with the same quote or the same id near the same line."""
    if f["key"] == prior["key"]:
        return True
    if rel_to(str(f["file"]), root) != rel_to(str(prior["file"]), root):
        return False
    return norm_ws(str(f["quote"])) == norm_ws(str(prior["quote"])) or (
        f["id"] == prior["id"] and abs(int(f["line"]) - int(prior["line"])) <= QUOTE_WINDOW)


def demote_late(judged: dict[str, Any], repo: Path, entry: dict[str, Any], tree: str, root: Path) -> None:
    """On a delta re-review, a new should-fix off the lines changed since its base, older or not, is kept as a nit.

    A blocker always stands, and so does an earlier finding re-reported because it is still not fixed."""
    spans: dict[str, list[tuple[int, int]]] = {}
    for f in judged["findings"]:
        if f["state"] != "open" or f["severity"] != LATE_SEVERITY or any(same_finding(f, p, root) for p in entry.get("prior", [])):
            continue
        path = rel_to(str(f["file"]), root)
        if path not in spans:
            spans[path] = changed_lines(repo, entry["base"], tree, path)
        if any(a - QUOTE_WINDOW <= int(f["line"]) <= b + QUOTE_WINDOW for a, b in spans[path]):
            continue
        f["severity"] = "nit"
        f["late"] = f"{LATE_SEVERITY} on lines unchanged since tree {entry['base'][:TREE_SHORT_LEN]}"
    if judged["status"] == "findings" and not any(blocks(f) for f in judged["findings"]):
        judged["status"] = "clean"


def ingest_round(repo: Path | None, round_dir: Path, stage_dir: Path, root: Path, tree: str | None) -> dict[str, Any]:
    """Judge every saved output of a round and score its canary; update reviewer clean state."""
    dispatch = load_json(round_dir / "dispatch.json", None)
    if dispatch is None:
        raise GateError(f"{round_dir} has no dispatch.json")
    rejections = load_json(stage_dir / gate_view.REJECTIONS_FILE, {})
    state = load_json(stage_dir / "state.json", {})
    canary = {"result": "unavailable"}
    if "canary" in dispatch:
        c = dispatch["canary"]
        out = Path(c["out"])
        canary = score_fixture(out.read_text(encoding="utf-8"), Path(c["fixture"]), c["id"],
                               Path(c["workspace"]) if c.get("workspace") else None) if out.exists() \
            else {"result": "invalid", "error": f"{gate_view.NOT_SAVED_ERROR}: {out}", "false_positives": 0}
        canary["id"] = c["id"]
    proved = canary["result"] in CANARY_OK      # a missed canary: the round proved nothing, re-dispatch all of it
    results = []
    for entry in dispatch["dispatch"]:
        out = Path(entry["out"])
        text = out.read_text(encoding="utf-8") if out.exists() else ""
        judged = judge_output(text, entry["id"], entry.get("files", []), repo, tree, root, rejections, dispatch["tree"],
                              allow_older=stage_dir.name == STAGE_REVIEW)
        if not out.exists():
            judged.update(status="invalid", error=f"{gate_view.NOT_SAVED_ERROR}: {out}")
        elif repo is not None and entry.get("base"):
            demote_late(judged, repo, entry, dispatch["tree"], root)
        results.append(judged)
        freeze_allowlist(text, stage_dir / f"allowlist-{entry['id']}.yaml")
        valid = proved and judged["status"] != "invalid"     # what the next round's delta starts from
        state[entry["id"]] = {"clean_tree": dispatch["tree"] if proved and judged["status"] == "clean" else None,
                              "round": round_dir.name,
                              "older": has_older_bug(judged["findings"]),
                              "valid_round": round_dir.name if valid else state.get(entry["id"], {}).get("valid_round")}
    report ={"round": round_dir.name, "tree": dispatch["tree"], "results": results, "canary": canary,
              "ingested_at": time.time(), "seconds": round(time.time() - dispatch.get("prepared_at", time.time()))}
    save_json(round_dir / "ingest.json", report)
    save_json(stage_dir / "state.json", state)
    return report


def print_ingest(report: dict[str, Any]) -> bool:
    """Print a round's findings; return True when nothing blocks."""
    ok = report["canary"]["result"] in CANARY_OK
    print(f"{report['round']}  tree {report['tree'][:TREE_SHORT_LEN]}  canary: {report['canary']}")
    for r in report["results"]:
        print(f"- {r['id']}: {r['status']}{('  ' + r['error']) if r['error'] else ''}")
        ok = ok and r["status"] == "clean"
        for i, f in enumerate(r["findings"]):
            late = f"  (kept as a nit: {f['late']})" if f.get("late") else ""
            print(f"    #{i} [{f['state']}] {f['severity']} {f['id']} {f['file']}:{f['line']}  {f.get('why', '')}{late}")
    return ok


def cmd_review_ingest(args: argparse.Namespace) -> int:
    """Verify a round's saved outputs; exit 0 only when every reviewer is clean and the canary held."""
    repo = repo_root(args.repo)
    stage_dir = review_dir(repo, args)
    round_dir = latest_round(stage_dir, args.round)
    report = ingest_round(repo, round_dir, stage_dir, repo, load_json(round_dir / "dispatch.json", {})["tree"])
    ok = print_ingest(report)
    if not ok and stage_dir.name == STAGE_REVIEW:
        count_failure(repo, STAGE_REVIEW, [r["id"] for r in report["results"] if r["status"] != "clean"])
    return EXIT_OK if ok else EXIT_FAIL


def cmd_reject(args: argparse.Namespace) -> int:
    """Reject one open finding with a file:line counter-citation; a re-raised finding can't be rejected."""
    repo = repo_root(args.repo)
    stage_dir = branch_dir(repo) / args.stage_dir
    round_dir = latest_round(stage_dir, args.round)
    report = load_json(round_dir / "ingest.json", None)
    if report is None:
        raise GateError("ingest the round before rejecting a finding")
    reviewer, _, idx = args.finding.partition("#")
    result = next((r for r in report["results"] if r["id"] == reviewer), None)
    if result is None or not idx.isdigit() or int(idx) >= len(result["findings"]):
        raise GateError(f"no finding {args.finding} in {round_dir.name}")
    finding = result["findings"][int(idx)]
    if finding["state"] != "open":
        raise GateError(f"finding is {finding['state']}; only an open finding can be rejected (once)")
    cite_file, _, cite_line = args.cite.rpartition(":")
    cited = repo / cite_file if not Path(cite_file).is_absolute() else Path(cite_file)
    if not cited.is_file() or not cite_line.isdigit() or int(cite_line) > len(cited.read_text(encoding="utf-8", errors="replace").splitlines()):
        raise GateError(f"counter-citation {args.cite} does not point at an existing line")
    rejections = load_json(stage_dir / gate_view.REJECTIONS_FILE, {})
    rejections[finding["key"]] = {"round": round_dir.name, "cite": args.cite, "reason": args.reason}
    save_json(stage_dir / gate_view.REJECTIONS_FILE, rejections)
    finding["state"] = "rejected"
    state = load_json(stage_dir / "state.json", {})
    latest = state.get(reviewer, {}).get("round") == round_dir.name   # rejecting in an earlier round leaves newer state alone
    if latest:
        state[reviewer]["older"] = has_older_bug(result["findings"])
    if not any(blocks(f) for f in result["findings"]) and result["status"] == "findings":
        result["status"] = "clean"
        if latest and report["canary"]["result"] in CANARY_OK:      # a round whose canary missed proved nothing
            state[reviewer]["clean_tree"] = report["tree"]
    save_json(stage_dir / "state.json", state)
    save_json(round_dir / "ingest.json", report)
    print(f"rejected {args.finding}: {args.reason} ({args.cite})")
    return EXIT_OK


def cmd_note(args: argparse.Namespace) -> int:
    """Record, for the dashboard only, a plain-English line for one finding and what was done about it."""
    repo = repo_root(args.repo)
    round_dir = latest_round(branch_dir(repo) / args.stage_dir, args.round)
    report = load_json(round_dir / "ingest.json", None)
    if report is None:
        raise GateError("ingest the round before noting a finding")
    reviewer, _, idx = args.finding.partition("#")
    result = next((r for r in report["results"] if r["id"] == reviewer), None)
    if result is None or not idx.isdigit() or int(idx) >= len(result["findings"]):
        raise GateError(f"no finding {args.finding} in {round_dir.name}")
    notes = load_json(round_dir / gate_view.NOTES_FILE, {})
    old = notes.get(args.finding, {})
    notes[args.finding] = {"plain": args.plain or old.get("plain", ""), "done": args.done or old.get("done", "")}
    save_json(round_dir / gate_view.NOTES_FILE, notes)
    print(f"noted {args.finding}")
    return EXIT_OK


def view_caps(cfg: dict[str, Any]) -> dict[str, int]:
    """Each stage's attempt cap, as the dashboard and the handoff show it."""
    return {stage: int(cfg["caps"][key]) for stage, key in CAP_KEYS.items() if key in cfg.get("caps", {})}


def write_handoff(repo: Path, note: str | None = None) -> Path:
    """Copy the run's request, plan and divergence log into the worktree and write HANDOFF.md from the gate state.

    The plan and the log move into `HANDOFF_DIR` and the run points at the copies from then on, so a session
    opened on the worktree edits the same files the reviewers read. Both are kept out of git.
    """
    run = run_info(repo)
    hdir = repo / HANDOFF_DIR
    hdir.mkdir(exist_ok=True)
    files = {}
    for key, name in HANDOFF_COPIES.items():
        src = Path(run[key]) if run.get(key) else None
        if src is None or not src.exists():
            continue
        dst = hdir / name
        if src.resolve() != dst.resolve():
            shutil.copyfile(src, dst)
            if key != "request":                     # the request stays registered in the gate's own state
                run[key] = str(dst)
        files[key] = str(dst.relative_to(repo))
    if note is not None:
        run["handoff_note"] = note
    save_json(branch_dir(repo) / "run.json", run)
    exclude = Path(common_dir(repo)) / "info" / "exclude"
    have = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    missing = [p for p in (HANDOFF_FILE, f"{HANDOFF_DIR}/") if p not in have]
    if missing:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write("".join(f"{p}\n" for p in missing))
    view = gate_view.summary(branch_dir(repo), view_caps(load_config(repo)))
    out = repo / HANDOFF_FILE
    out.write_text(gate_view.handoff(view, repo, files, run.get("handoff_note", "")), encoding="utf-8")
    return out


def announce_handoff(repo: Path) -> None:
    """Write the handoff and tell the agent how the user continues in the worktree's own window."""
    path = write_handoff(repo)
    print(f"handoff: {path}\nAsk the user: build here, or in the worktree's own window? For the window: run "
          f"`code {repo}`, and the user types /resume-handoff there.")


def cmd_handoff(args: argparse.Namespace) -> int:
    """Write or refresh the worktree's HANDOFF.md (also done after a plan stamp and when a stage's tries run out)."""
    repo = repo_root(args.repo)
    write_handoff(repo, args.note)
    print(f"handoff: {repo / HANDOFF_FILE}")
    return EXIT_OK


# --------------------------------------------------------------------------- plan gate

def plan_table_paths(text: str) -> list[str]:
    """Backticked repo paths in the first column of a plan's markdown tables, as globs."""
    out: list[str] = []
    for line in text.splitlines():
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = line.split("|")
        if len(cells) < 3:
            continue
        for raw in re.findall(r"`([^`]+)`", cells[1]):
            out.extend(globify(raw))
    return out


def globify(raw: str) -> list[str]:
    """Turn a cited path (with <placeholders>, … or a trailing /) into match patterns; skip non-repo paths."""
    p = raw.strip()
    if not p or p.startswith(("~", "/", "-", "$")) or ("/" not in p and "." not in p):
        return []
    p = re.sub(r"<[^>]+>", "*", p).replace("…", "**").replace("...", "**")
    p = re.sub(r"\{[^}]*\}", "*", p)
    return [p.rstrip("/") + "/**"] if p.endswith("/") else [p]


def divergence_paths(text: str) -> list[str]:
    """Every backticked path in a divergence log."""
    return [g for raw in re.findall(r"`([^`]+)`", text) for g in globify(raw)]


def cmd_plan_prepare(args: argparse.Namespace) -> int:
    """Tier a plan from its file table; write the plan reviewer's prompt and a canary."""
    repo = repo_root(args.repo)
    cfg = load_config(repo)
    plan = Path(args.plan).resolve()
    tier, reason = compute_tier(cfg, plan_table_paths(plan.read_text(encoding="utf-8")), None, None)
    result: dict[str, Any] = {"plan": str(plan), "tier": tier, "tier_reason": reason, "dispatch": []}
    reviewer = next(r for r in all_reviewers(cfg, repo) if r.stage == STAGE_PLAN)
    if tier not in reviewer.tiers:
        result["note"] = f"no plan reviewer at tier {tier}; run `stamp --stage plan`"
        print(json.dumps(result, indent=2))
        return EXIT_OK
    if missing := missing_source(reviewer):
        raise GateError(f"plan reviewer files missing: {missing}")
    why = calibration_problem(reviewer, repo)
    if why:
        print(f"gate: warning, plan reviewer not calibrated: {why}", file=sys.stderr)
    check_cap(repo, STAGE_PLAN, cfg)
    stage_dir = branch_dir(repo) / STAGE_PLAN
    n, round_dir = next_round(stage_dir)
    seed = sha256_file(plan)
    ctx = review_ctx(repo, cfg, reviewer, [], seed, str(n), round_dir / "none", stage_dir)
    ctx.update(PLAN=str(plan), DIFF="NONE", FILES="NONE", CHANGED_FILES="NONE")
    result["dispatch"].append(write_dispatch(round_dir, reviewer, ctx, reviewer.model_for(tier), reviewer.id))
    result.update(round=n, tree=seed)
    save_json(round_dir / "dispatch.json", result | {"prepared_at": time.time()})
    print(json.dumps(result, indent=2))
    return EXIT_OK


def cmd_plan_ingest(args: argparse.Namespace) -> int:
    """Verify the plan reviewer's output; quotes are checked against the files on disk."""
    repo = repo_root(args.repo)
    stage_dir = branch_dir(repo) / STAGE_PLAN
    round_dir = latest_round(stage_dir, args.round)
    report = ingest_round(None, round_dir, stage_dir, repo, None)
    ok = print_ingest(report)
    if not ok:
        count_failure(repo, STAGE_PLAN)
    return EXIT_OK if ok else EXIT_FAIL


# --------------------------------------------------------------------------- checks

# pytest's short summary (`FAILED <id>`, `ERROR <id>`) and unittest's headers (`FAIL: <id>`, `ERROR: <id>`), not its `FAILED (failures=1)`.
FAILED_TEST = re.compile(r"^(?:(?:FAILED|ERROR) (?!\()|(?:FAIL|ERROR): (?=\S+ \())(\S+)", re.MULTILINE)


def targeted_tests(repo: Path, cfg: dict[str, Any], files: list[str]) -> list[str]:
    """Test files for the changed files: the [tests] map, changed tests, and tests importing a changed module."""
    found: set[str] = set()
    patterns = cfg.get("tests", {}).get("map", [])
    modules = []
    for f in files:
        p = Path(f)
        if p.suffix != ".py":
            continue
        if p.name.startswith("test_") and (repo / f).exists():
            found.add(f)
        for pat in patterns:
            cand = pat.replace("{stem}", p.stem)
            if (repo / cand).exists():
                found.add(cand)
        parts = list(p.with_suffix("").parts)
        if parts and parts[0] == "src":
            parts = parts[1:]
        if parts and not p.name.startswith("test_"):
            modules.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    if modules:
        for test in repo.glob("tests/**/test_*.py"):
            text = test.read_text(encoding="utf-8", errors="replace")
            if any(re.search(rf"\b(?:from|import)\s+{re.escape(m)}\b", text) for m in modules):
                found.add(str(test.relative_to(repo)))
    return sorted(found)


def render_check(c: Check, repo: Path, cfg: dict[str, Any], files: list[str], run_dir: Path) -> str | None:
    """The check's command with its placeholders filled, or None when it has nothing to run."""
    cmd = c.run
    if "{targeted_tests}" in cmd:
        tests = targeted_tests(repo, cfg, files)
        if not tests:
            return None
        cmd = cmd.replace("{targeted_tests}", " ".join(shlex.quote(t) for t in tests))
    return cmd.replace("{run_dir}", str(run_dir)).replace("{root}", str(repo))


def run_command(cmd: str, cwd: Path, log: Path, env: dict[str, str] | None = None) -> tuple[int, float, str]:
    """Run a shell command, capture its output to `log`; return (exit code, seconds, output)."""
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=CHECK_TIMEOUT_S,
                              executable="/bin/bash", env=env)
        code, output = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        code, output = EXIT_TIMEOUT, f"timeout after {CHECK_TIMEOUT_S}s\n{exc.stdout or ''}"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"$ {cmd}\n{output}\nexit {code}\n", encoding="utf-8")
    return code, round(time.monotonic() - t0, 1), output


def check_clean(repo: Path, c: Check, entry: dict[str, Any] | None, current: str, cfg: dict[str, Any]) -> bool:
    """A `last` check must have passed on exactly this tree; others while no file in their scope changed."""
    if not entry or not entry.get("pass_tree"):
        return False
    if c.last:
        return entry["pass_tree"] == current
    return still_clean(repo, entry["pass_tree"], current, expand_globs(c.when, cfg["globs"]))


def cmd_checks_run(args: argparse.Namespace) -> int:
    """Run every selected check not yet passing for this tree, in kind order; stop later groups on a failure."""
    repo = repo_root(args.repo)
    cfg = load_config(repo)
    target = staged_target(repo, cfg)
    if args.baseline:
        return run_baseline(repo, cfg, args.baseline, target)
    tier, _ = branch_tier(repo, cfg, target)
    state_path = branch_dir(repo) / STAGE_CHECKS / "state.json"
    state = load_json(state_path, {})
    todo = [(c, p) for c, p in pick_checks(cfg, target.files, tier)
            if p.selected and not check_clean(repo, c, state.get(c.id), target.end, cfg)]
    if not todo:
        print("every selected check passes for this tree; run `stamp --stage checks`")
        return EXIT_OK
    check_cap(repo, STAGE_CHECKS, cfg)
    run_dir = branch_dir(repo) / STAGE_CHECKS / "run" / target.end[:RUN_DIR_TREE_LEN]
    run_dir.mkdir(parents=True, exist_ok=True)
    failed_group: int | None = None
    ok = True
    for c, _ in todo:
        log = tree_dir(repo, target.end) / STAGE_CHECKS / f"{c.id}.log"
        if failed_group is not None and c.group() > failed_group:
            print(f"- {c.id}: not run, an earlier group failed")
            continue
        cmd = render_check(c, repo, cfg, target.files, run_dir)
        if cmd is None:
            state[c.id] = {"pass_tree": target.end, "status": STATUS_SKIPPED, "reason": "no mapped tests"}
            print(f"- {c.id}: SKIPPED (no mapped tests)")
            continue
        code, seconds, output = run_command(cmd, repo, log)
        status = STATUS_PASS if code == 0 else STATUS_FAIL
        state[c.id] = {"pass_tree": target.end if code == 0 else None, "status": status, "log": str(log),
                       "seconds": seconds, "failed_ids": FAILED_TEST.findall(output), "tree": target.end}
        print(f"- {c.id}: {status.upper()} in {seconds}s  ({log})")
        if code != 0:
            ok = False
            failed_group = c.group()
    save_json(state_path, state)
    if not ok:
        count_failure(repo, STAGE_CHECKS)
    return EXIT_OK if ok else EXIT_FAIL


def temp_worktree(repo: Path, cfg: dict[str, Any], ref: str, merge: str | None = None) -> tuple[Path, dict[str, str]]:
    """A detached temp worktree at `ref` (optionally with `merge` merged, uncommitted), plus its env."""
    tmp = Path(tempfile.mkdtemp(prefix="gate-wt-")) / "wt"
    run_git(repo, "worktree", "add", "--detach", "--quiet", str(tmp), ref)
    if merge:
        try:
            run_git(tmp, "merge", "--no-commit", "--no-ff", "--quiet", merge)
        except GateError:
            drop_worktree(repo, tmp)
            raise
    for link in cfg.get("baseline", {}).get("link", []):
        if (repo / link).exists() and not (tmp / link).exists():
            os.symlink(repo / link, tmp / link)
    env = dict(os.environ)
    for key, value in cfg.get("baseline", {}).get("env", {}).items():
        env[key] = str(value).replace("{root}", str(tmp))
    return tmp, env


def drop_worktree(repo: Path, tmp: Path) -> None:
    """Remove a temp worktree created by temp_worktree."""
    run_git(repo, "worktree", "remove", "--force", str(tmp), check=False)
    shutil.rmtree(tmp.parent, ignore_errors=True)


def run_baseline(repo: Path, cfg: dict[str, Any], check_id: str, target: Target) -> int:
    """Re-run one failing check on a clean base worktree: tell 'I broke it' from 'already broken'.

    A diagnosis only: the check stays failing either way, because a red test blocks the commit even when
    base is red too.
    """
    check = next((c for c in all_checks(cfg) if c.id == check_id), None)
    if check is None:
        raise GateError(f"no check {check_id}")
    state_path = branch_dir(repo) / STAGE_CHECKS / "state.json"
    state = load_json(state_path, {})
    entry = state.get(check_id, {})
    if entry.get("status") != STATUS_FAIL or entry.get("tree") != target.end:
        raise GateError(f"{check_id} has no failure recorded on this tree; run checks-run first")
    mine = set(entry.get("failed_ids", []))
    tmp, env = temp_worktree(repo, cfg, base_ref(repo, cfg))
    try:
        cmd = render_check(check, tmp, cfg, target.files, tmp) or "true"
        code, _, output = run_command(cmd, tmp, tree_dir(repo, target.end) / STAGE_CHECKS / f"{check_id}.baseline.log", env)
    finally:
        drop_worktree(repo, tmp)
    base_failed = set(FAILED_TEST.findall(output))
    if code == 0:
        verdict = "base is green, so the failure is yours: fix the code"
    elif mine and not (mine - base_failed):
        verdict = "every failing test also fails on base: already broken there, still blocking: fix it or ask the user"
    elif not mine and not base_failed:
        verdict = "base fails too, but with no test ids to compare: fix it or ask the user"
    else:
        verdict = f"new failures, yours: {sorted(mine - base_failed)}"
    state[check_id] = entry | {"note": verdict}
    save_json(state_path, state)
    print(f"{check_id}: {verdict}")
    return EXIT_FAIL


# --------------------------------------------------------------------------- audit

def added_lines(repo: Path, *diff_args: str) -> list[tuple[str, str]]:
    """(file, line) for every added line in a diff."""
    out: list[tuple[str, str]] = []
    current = ""
    for line in run_git(repo, "diff", "-U0", "--no-renames", *diff_args).splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("+") and current:
            out.append((current, line[1:]))
    return out


def planned_patterns(repo: Path) -> list[str] | None:
    """Paths the registered plan and divergence log allow, or None when no plan is registered."""
    run = run_info(repo)
    plan = Path(run["plan"]) if run.get("plan") else None
    if plan is None or not plan.exists():
        return None
    patterns = plan_table_paths(plan.read_text(encoding="utf-8"))
    log = Path(run["divergence_log"]) if run.get("divergence_log") else None
    if log and log.exists():
        patterns += divergence_paths(log.read_text(encoding="utf-8"))
    return patterns


def hygiene_hits(repo: Path, cfg: dict[str, Any], files: list[str], binary: list[str],
                 added: list[tuple[str, str]], size_of: Callable[[str], int | None]) -> tuple[list[dict[str, str]], list[str]]:
    """H1-H4 over a set of changed files; returns (hits, notes)."""
    commit = cfg["commit"]
    junk = expand_globs(commit["junk_patterns"], cfg["globs"])
    planned = planned_patterns(repo)
    notes = [] if planned is not None else ["H1 skipped: no plan registered (gate.py init --plan); "
                                            "H4 then blocks every binary or heavy file, since no plan lists it"]
    hits = []
    for f in files:
        in_plan = planned is not None and first_match(f, planned) is not None
        if planned is not None and not in_plan:
            hits.append({"rule": "H1 unplanned", "file": f, "detail": "not in the plan's file table or the divergence log"})
        size = size_of(f)
        if size is None:               # deleted: removing junk or a heavy file is the cleanup, not the problem
            continue
        pat = first_match(f, junk)
        if pat:
            hits.append({"rule": "H2 junk", "file": f, "detail": f"matches {pat}"})
        if not in_plan and f in binary:
            hits.append({"rule": "H4 heavy", "file": f, "detail": "binary file not in the plan"})
        elif not in_plan and size is not None and size > commit["max_file_kb"] * 1024:
            hits.append({"rule": "H4 heavy", "file": f, "detail": f"{size // 1024} KB > {commit['max_file_kb']} KB"})
    for f, line in added:
        marker = next((m for m in commit["scratch_markers"] if m in line), None)
        if marker:
            hits.append({"rule": "H3 scratch leak", "file": f, "detail": f"added line contains {marker!r}"})
    return hits, notes


RULE_SECRET = "P2 secret"            # the one audit hit #accept-gate never lets through


def hit_text(h: dict[str, str]) -> str:
    """How an audit hit is listed in a push stamp's `open` problems."""
    return f"{h['rule']}: {h['file']}: {h['detail']}"


def blob_size(repo: Path, spec: str) -> int | None:
    """Size of a blob, or None when the path is deleted."""
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "-s", spec], capture_output=True, text=True)
    return int(proc.stdout.strip()) if proc.returncode == 0 else None


def push_hits(repo: Path, cfg: dict[str, Any], files: list[str], added: list[tuple[str, str]]) -> list[dict[str, str]]:
    """Forbidden paths among the present `files`, and secret patterns in the added lines."""
    hits = []
    forbid = expand_globs(cfg["push"]["forbid_paths"], cfg["globs"])
    templates = tuple(cfg["push"]["template_suffixes"])
    for f in files:
        pat = first_match(f, forbid)
        if pat and not f.endswith(templates):
            hits.append({"rule": "P1 forbidden path", "file": f, "detail": f"matches {pat}"})
    secrets = [re.compile(s) for s in cfg["push"]["secret_patterns"]]
    for f, line in added:
        for rx in secrets:
            if rx.search(line):
                hits.append({"rule": RULE_SECRET, "file": f, "detail": f"added line matches /{rx.pattern}/"})
    return hits


def freshness_hits(repo: Path, cfg: dict[str, Any], files: list[str], fetch: bool) -> list[dict[str, str]]:
    """Base moved under the branch: conflict ⇒ hit; overlap ⇒ re-run the affected checks on the merge."""
    if fetch:
        proc = subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin", cfg.get("base", "main")],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            return [{"rule": "P3 stale", "file": "-",
                     "detail": f"could not fetch origin/{cfg.get('base', 'main')}, freshness unproven: {proc.stderr.strip()}"}]
    ref = base_ref(repo, cfg)
    if git_ok(repo, "merge-base", "--is-ancestor", ref, "HEAD"):
        return []
    mb = run_git(repo, "merge-base", "HEAD", ref).strip()
    if "<<<<<<<" in run_git(repo, "merge-tree", mb, "HEAD", ref, check=False):
        return [{"rule": "P3 stale", "file": "-", "detail": f"conflicts with {ref}: needs a rebase, outside the authorization", "conflict": True}]
    moved = run_git(repo, "diff", "--name-only", "--no-renames", mb, ref).splitlines()
    dirs = {str(Path(f).parent) for f in files}
    if not [m for m in moved if m in files or str(Path(m).parent) in dirs]:
        return []
    union = sorted(set(files) | set(moved))
    checks = [c for c in all_checks(cfg) if c.kind != "real"
              and any(first_match(f, expand_globs(c.when, cfg["globs"])) for f in union)]
    try:
        tmp, env = temp_worktree(repo, cfg, "HEAD", merge=ref)
    except GateError as exc:
        return [{"rule": "P3 stale", "file": "-", "detail": f"merging {ref} conflicts: {exc}", "conflict": True}]
    hits = []
    try:
        for c in sorted(checks, key=Check.group):
            cmd = render_check(c, tmp, cfg, union, tmp)
            if cmd is None:
                continue
            code, _, _ = run_command(cmd, tmp, tree_dir(repo, head_tree(repo)) / STAGE_CHECKS / f"{c.id}.merged.log", env)
            if code != 0:
                hits.append({"rule": "P3 stale", "file": "-", "detail": f"{c.id} fails once {ref} is merged in"})
    finally:
        drop_worktree(repo, tmp)
    return hits


def branch_commit_hits(repo: Path, cfg: dict[str, Any], mb: str) -> tuple[list[dict[str, str]], list[str], list[str]]:
    """H1-H4, forbidden paths and secrets on every commit's own diff, so a file added then removed is still seen."""
    hits: list[dict[str, str]] = []
    notes: list[str] = []
    seen: set[str] = set()
    files: set[str] = set()
    for commit in run_git(repo, "rev-list", "--reverse", "--no-merges", f"{mb}..HEAD").split():
        parent = f"{commit}^" if git_ok(repo, "rev-parse", "--verify", "--quiet", f"{commit}^") else EMPTY_TREE
        changed, _, binary = numstat(repo, parent, commit)
        added = added_lines(repo, parent, commit)
        sizes = {f: blob_size(repo, f"{commit}:{f}") for f in changed}
        found, more = hygiene_hits(repo, cfg, changed, binary, added, sizes.get)
        present = [f for f in changed if sizes[f] is not None]
        for h in found + push_hits(repo, cfg, present, added):
            key = f"{h['rule']}|{h['file']}|{h['detail']}"
            if key not in seen:
                seen.add(key)
                hits.append(h | {"commit": commit[:TREE_SHORT_LEN]})
        notes += [n for n in more if n not in notes]
        files.update(changed)
    return hits, notes, sorted(files)


def cmd_audit(args: argparse.Namespace) -> int:
    """Commit hygiene (--mode staged) or the pre-push audit (--mode branch); exit 1 on any hit."""
    repo = repo_root(args.repo)
    cfg = load_config(repo)
    if args.mode == AUDIT_STAGED:
        parent = "HEAD" if git_ok(repo, "rev-parse", "--verify", "--quiet", "HEAD") else EMPTY_TREE
        files, _, binary = numstat(repo, "--cached", parent)
        added = added_lines(repo, "--cached", parent)
        hits, notes = hygiene_hits(repo, cfg, files, binary, added, lambda f: blob_size(repo, f":{f}"))
        key = index_tree(repo)
    else:
        if not args.guard:
            check_cap(repo, STAGE_PUSH, cfg)
        hits, notes, files = branch_commit_hits(repo, cfg, merge_base(repo, cfg))
        if not args.no_freshness:
            hits += freshness_hits(repo, cfg, files, args.fetch)
        key = head_tree(repo)
    accepted: list[dict[str, str]] = []
    push = load_json(tree_dir(repo, key) / f"{STAGE_PUSH}.json", {})
    if args.mode == AUDIT_BRANCH and push.get("verdict") == VERDICT_ACCEPTED:
        listed = set(push.get("open", []))
        accepted = [h for h in hits if h["rule"] != RULE_SECRET and hit_text(h) in listed]
        hits = [h for h in hits if h not in accepted]
    leftovers = run_git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
    report = {"mode": args.mode, "tree": key, "files": files, "hits": hits, "accepted": accepted, "notes": notes,
              "leftovers": leftovers, "at": now_iso()}
    save_json(tree_dir(repo, key) / f"audit-{args.mode}.json", report)
    for h in hits:
        print(f"BLOCK {hit_text(h)}")
    for h in accepted:
        print(f"accepted by the user (#accept-gate): {hit_text(h)}")
    for n in notes:
        print(f"note: {n}")
    if leftovers:
        print("H5 leftovers (untracked, not committed; clean up): " + ", ".join(leftovers))
    print(f"audit --{args.mode}: {'clean' if not hits else str(len(hits)) + ' hit(s)'} ({len(files)} files)")
    if hits and args.mode == AUDIT_BRANCH and not args.guard:
        count_failure(repo, STAGE_PUSH)
        if any(h.get("conflict") for h in hits):        # only a rebase can fix it: no attempt left to try
            while load_json(branch_dir(repo) / "attempts.json", {}).get(STAGE_PUSH, 0) < int(cfg["caps"][CAP_KEYS[STAGE_PUSH]]):
                count_failure(repo, STAGE_PUSH)
    return EXIT_OK if not hits else EXIT_FAIL


# --------------------------------------------------------------------------- stamps

def rounds_since_stamp(stage_dir: Path, whole_branch: bool = False) -> list[dict[str, Any]]:
    """Ingest reports of the rounds after the last stamp on this branch, or of every round on it."""
    since = 0 if whole_branch else load_json(stage_dir / "stamped.json", {}).get("after_round", 0)
    out = []
    for p in sorted(stage_dir.glob("round-*"), key=lambda p: int(p.name.split("-")[1])):
        if int(p.name.split("-")[1]) > since and (p / "ingest.json").exists():
            out.append(load_json(p / "ingest.json", {}))
    return out


def open_findings(rounds: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(open blocking, open nits) from each reviewer's latest result."""
    latest: dict[str, dict[str, Any]] = {}
    for rep in rounds:
        for r in rep["results"]:
            latest[r["id"]] = r
    blocking, nits = [], []
    for rid, r in latest.items():
        for f in r["findings"]:
            if f["state"] in ("open", "reraised"):
                (blocking if f["severity"] in BLOCKING else nits).append({"reviewer": rid} | f)
    return blocking, nits


def finish(problems: list[str], accept: bool) -> str:
    """PASS, ACCEPTED (user token consumed), or raise with the open problems."""
    if not problems:
        return VERDICT_PASS
    if accept:
        if take_token(TOKEN_ACCEPT) is None:
            raise GateError("no armed #accept-gate token: only the user can arm one", EXIT_FAIL)
        return VERDICT_ACCEPTED
    raise GateError("cannot stamp:\n  " + "\n  ".join(problems), EXIT_FAIL)


def mark_stamped(stage_dir: Path, stage: str, repo: Path) -> None:
    """Remember the last round covered by a stamp and give the stage fresh attempts."""
    rounds = [int(p.name.split("-")[1]) for p in stage_dir.glob("round-*")]
    save_json(stage_dir / "stamped.json", {"after_round": max(rounds, default=0), "at": now_iso()})
    reset_attempt(repo, stage)


def stamp_review(repo: Path, cfg: dict[str, Any], accept: bool) -> dict[str, Any]:
    """Every selected reviewer clean for this tree, and the last round's canary held."""
    target = staged_target(repo, cfg)
    tier, reason, picks = review_selection(repo, cfg, target, argparse.Namespace(only=None, tripwire=False))
    stage_dir = branch_dir(repo) / STAGE_REVIEW
    state = load_json(stage_dir / "state.json", {})
    selected = [(r, p) for r, p in picks if p.selected]
    problems = [f"{r.id}: not clean for this tree" for r, _ in selected
                if not stays_clean(repo, cfg, r, state, target.end)]
    new = rounds_since_stamp(stage_dir)
    if new and new[-1]["canary"]["result"] not in CANARY_OK:
        problems.append(f"canary {new[-1]['canary']['result']} in {new[-1]['round']}: re-run the round")
    older = gate_view.summary(branch_dir(repo), view_caps(cfg), None, repo)["older"]
    if older:
        problems.append(f"{len(older)} older bug(s) wait for the user's decision: `fix them` or `#accept-gate`")
    rounds = rounds_since_stamp(stage_dir, whole_branch=True)    # the MR covers the whole branch, not one commit
    blocking, nits = open_findings(rounds)
    verdict = finish(problems, accept)
    decided = load_json(stage_dir / gate_view.OLDER_DECIDED_FILE, []) + [f["key"] for f in older]
    if older:                                                    # accepted: the user does not get asked again
        save_json(stage_dir / gate_view.OLDER_DECIDED_FILE, decided)
    stamp = {"stage": STAGE_REVIEW, "tree": target.end, "verdict": verdict, "tier": tier, "tier_reason": reason,
             "selected": [{"id": r.id, "reason": p.reason, "model": last_dispatched_model(r, tier, stage_dir, state)}
                          for r, p in selected],
             "skipped": [{"id": p.id, "reason": p.reason} for _, p in picks if not p.selected],
             "rounds": [{"round": x["round"], "reviewers": [r["id"] for r in x["results"]],
                         "canary": x["canary"], "seconds": x.get("seconds"),
                         "blocking_found": sum(1 for r in x["results"] for f in r["findings"]
                                               if f["severity"] in BLOCKING and f["state"] != "dropped" and not f.get("older")),
                         "dropped": sum(1 for r in x["results"] for f in r["findings"] if f["state"] == "dropped")}
                        for x in rounds],
             "open_nits": nits, "at": now_iso(),
             "open_blocking": blocking if verdict == VERDICT_ACCEPTED else [b for b in blocking if b.get("key") in decided]}
    mark_stamped(stage_dir, STAGE_REVIEW, repo)
    return stamp


def stamp_checks(repo: Path, cfg: dict[str, Any], accept: bool) -> dict[str, Any]:
    """Every selected check passing (or skipped) for this tree."""
    target = staged_target(repo, cfg)
    tier, _ = branch_tier(repo, cfg, target)
    state = load_json(branch_dir(repo) / STAGE_CHECKS / "state.json", {})
    picks = pick_checks(cfg, target.files, tier)
    rows, problems = [], []
    for c, p in picks:
        if not p.selected:
            rows.append({"id": c.id, "kind": c.kind, "status": STATUS_SKIPPED, "reason": p.reason})
            continue
        entry = state.get(c.id, {})
        if not check_clean(repo, c, entry, target.end, cfg):
            problems.append(f"{c.id}: not passing for this tree ({entry.get('status', 'never run')})")
        rows.append({"id": c.id, "kind": c.kind, "status": entry.get("status", "never run"), "reason": p.reason,
                     "log": entry.get("log"), "seconds": entry.get("seconds"), "note": entry.get("reason") or entry.get("note")})
    verdict = finish(problems, accept)
    reset_attempt(repo, STAGE_CHECKS)
    return {"stage": STAGE_CHECKS, "tree": target.end, "verdict": verdict, "tier": tier, "checks": rows,
            "open": problems if verdict == VERDICT_ACCEPTED else [], "at": now_iso()}


def stamp_plan(repo: Path, cfg: dict[str, Any], plan: Path, accept: bool, approve: bool = False) -> dict[str, Any]:
    """The plan reviewer clean on this exact plan text (or no reviewer at its tier), or the user's approval.

    `approve` is the user's #approve-plan: a plan round reviewed an earlier text of this plan,
    the agent fixed its findings, and the user approved the fix instead of another round.
    """
    seed = sha256_file(plan)
    tier, reason = compute_tier(cfg, plan_table_paths(plan.read_text(encoding="utf-8")), None, None)
    stage_dir = branch_dir(repo) / STAGE_PLAN
    reviewer = next(r for r in all_reviewers(cfg, repo) if r.stage == STAGE_PLAN)
    rounds = rounds_since_stamp(stage_dir)
    problems = []
    if tier in reviewer.tiers:
        mine = [x for x in rounds if x["tree"] == seed]
        if not mine:
            problems.append("no plan round has reviewed this exact plan text")
        else:
            last = mine[-1]
            if any(r["status"] != "clean" for r in last["results"]):
                problems.append(f"plan reviewer not clean in {last['round']}")
            if last["canary"]["result"] not in CANARY_OK:
                problems.append(f"canary {last['canary']['result']} in {last['round']}")
    blocking, nits = open_findings(rounds)
    approved_after = None
    if problems and approve:
        reviewed = [x for x in rounds if x["canary"]["result"] in CANARY_OK]
        if not reviewed:
            raise GateError("cannot approve: no plan round reviewed this plan yet", EXIT_FAIL)
        if take_token(TOKEN_APPROVE_PLAN) is None:
            raise GateError("no armed #approve-plan token: only the user can arm one", EXIT_FAIL)
        verdict, approved_after = VERDICT_APPROVED, reviewed[-1]["round"]
    else:
        verdict = finish(problems, accept)
    mark_stamped(stage_dir, STAGE_PLAN, repo)
    run = run_info(repo)
    run.update(plan_tier=tier, plan_tier_reason=reason)
    save_json(branch_dir(repo) / "run.json", run)
    return {"stage": STAGE_PLAN, "plan": str(plan), "sha": seed, "verdict": verdict, "tier": tier, "tier_reason": reason,
            "open_nits": nits, "open_blocking": blocking if verdict == VERDICT_ACCEPTED else [],
            "fixed_and_approved": blocking if verdict == VERDICT_APPROVED else [], "approved_after": approved_after,
            "at": now_iso()}


def stamp_push(repo: Path, cfg: dict[str, Any], accept: bool) -> dict[str, Any]:
    """Review and checks stamped for HEAD's tree, and a clean branch audit on it."""
    tree = head_tree(repo)
    tdir = tree_dir(repo, tree)
    waived = SKIP_WAIVES.get(load_json(tdir / USER_SKIP_FILE, {}).get("kind"), ())
    problems = [f"no passing {s} stamp for HEAD's tree" for s in (STAGE_REVIEW, STAGE_CHECKS) if s not in waived
                and load_json(tdir / f"{s}.json", {}).get("verdict") not in PASSING]
    audit = load_json(tdir / "audit-branch.json", None)
    if audit is None:
        problems.append("no `audit --branch` for HEAD's tree")
    elif audit["hits"]:
        problems += [hit_text(h) for h in audit["hits"]]
        if accept and any(h["rule"] == RULE_SECRET for h in audit["hits"]):
            raise GateError("a secret in the branch history can't be accepted: only rewriting the history removes "
                            "it, and that is the user's call", EXIT_FAIL)
    verdict = finish(problems, accept)
    reset_attempt(repo, STAGE_PUSH)
    return {"stage": STAGE_PUSH, "tree": tree, "verdict": verdict, "open": problems if verdict == VERDICT_ACCEPTED else [],
            "at": now_iso()}


def cmd_stamp(args: argparse.Namespace) -> int:
    """Write a stage's stamp, or explain why it can't be written."""
    repo = repo_root(args.repo)
    cfg = load_config(repo)
    if args.approve and args.stage != STAGE_PLAN:
        raise GateError("--approve applies to the plan stage only")
    if args.stage == STAGE_PLAN:
        if not args.plan:
            raise GateError("--plan is required for the plan stamp")
        stamp = stamp_plan(repo, cfg, Path(args.plan).resolve(), args.accept, args.approve)
        save_json(state_root(repo) / "plans" / f"{stamp['sha']}.json", stamp)
        if stamp["verdict"] in PASSING:
            print(f"{args.stage}: {stamp['verdict']} ({stamp['sha']})")
            announce_handoff(repo)
            return EXIT_OK
    else:
        stamp = {STAGE_REVIEW: stamp_review, STAGE_CHECKS: stamp_checks, STAGE_PUSH: stamp_push}[args.stage](repo, cfg, args.accept)
        save_json(tree_dir(repo, stamp["tree"]) / f"{args.stage}.json", stamp)
    print(f"{args.stage}: {stamp['verdict']} ({stamp.get('tree') or stamp.get('sha')})")
    return EXIT_OK


# --------------------------------------------------------------------------- summary, status, explain

def check_result(c: dict[str, Any]) -> str:
    """One check that ran, as "How it was tested" names it; only a failure needs its log."""
    return (f"{c['id']} {c['status'].upper()}" + (f" [{c['note']}]" if c.get("note") else "")
            + (f" {c['seconds']}s" if c.get("seconds") else "")
            + (f", log {c['log']}" if c["status"] == STATUS_FAIL and c.get("log") else ""))


def tested_summary(cfg: dict[str, Any], tree: str, checks: dict[str, Any] | None, review: dict[str, Any] | None,
                   skip: dict[str, Any] | None) -> str:
    """"How it was tested", in a few lines: each kind that took part, then what is missing."""
    check_s = sum(c.get("seconds") or 0 for c in (checks or {}).get("checks", []))
    review_s = sum(x.get("seconds") or 0 for x in (review or {}).get("rounds", []))
    lines = [f"How it was tested (tree {tree[:TREE_SHORT_LEN]}, gate time {round(check_s + review_s)}s):"]
    missing = [] if checks else ["checks (no checks stamp)"]
    undeclared = []
    for kind in cfg.get("report_kinds", []):
        mine = [c for c in (checks or {}).get("checks", []) if c["kind"] == kind]
        ran = [c for c in mine if c["status"] != STATUS_SKIPPED]
        if kind == "review":
            continue
        if ran:
            lines.append(f"- {kind}: " + ", ".join(check_result(c) for c in ran))
        elif mine:
            missing.append(f"{kind} (" + "; ".join(f"{c['id']}: {c.get('note') or c['reason']}" for c in mine) + ")")
        elif checks:
            undeclared.append(kind)
    if "review" in cfg.get("report_kinds", []):
        if review:
            found = sum(x["blocking_found"] for x in review["rounds"])
            lines.append(f"- review: {review['verdict']}, {len(review['selected'])} reviewers, {len(review['rounds'])} rounds, "
                         f"{found} blocking found and resolved, {len(review['open_nits'])} nits open")
        else:
            missing.append("review (no review stamp)")
    if skip:
        missing.append(f"{skip.get('kind')} (skipped at your request)")
    if undeclared:
        missing.append("no check declared for " + ", ".join(undeclared))
    if missing:
        lines.append("- missing: " + "; ".join(missing))
    return "\n".join(lines)


def cmd_summary(args: argparse.Namespace) -> int:
    """Print the session's How-it-was-tested lines (--tested) or the full MR summary block."""
    repo = repo_root(args.repo)
    cfg = load_config(repo)
    tree = head_tree(repo) if args.head else index_tree(repo)
    tdir = tree_dir(repo, tree)
    review, checks = load_json(tdir / "review.json", None), load_json(tdir / "checks.json", None)
    skip = load_json(tdir / USER_SKIP_FILE, None)
    tested = tested_summary(cfg, tree, checks, review, skip)
    if args.tested:
        print(tested)
        return EXIT_OK
    lines = ["## Gate summary", ""]
    if review:
        lines += [f"Review: **{review['verdict']}**, tier {review['tier']} ({review['tier_reason']})", "",
                  "| reviewer | why it ran | model tier |", "| --- | --- | --- |"]
        lines += [f"| {s['id']} | {s['reason']} | {s['model']} |" for s in review["selected"]]
        lines += ["", "Skipped: " + "; ".join(f"{s['id']} ({s['reason']})" for s in review["skipped"])]
        if review["open_nits"]:
            lines += ["", "Open nits:"] + [f"- {n['reviewer']} {n['id']} {n['file']}:{n['line']}: {n.get('why', '')}"
                                          for n in review["open_nits"]]
        if review["open_blocking"]:
            lines += ["", "Accepted by the user with these open:"] + [
                f"- {b['reviewer']} {b['id']} {b['file']}:{b['line']}: {b.get('why', '')}" for b in review["open_blocking"]]
    if checks:
        lines += ["", "| check | kind | why it ran or was skipped | result |", "| --- | --- | --- | --- |"]
        lines += [f"| {c['id']} | {c['kind']} | {c['reason']} | {c['status']}"
                  + (f" ({c['note']})" if c.get("note") else "") + " |" for c in checks["checks"]]
    leftovers = sorted({f for mode in (AUDIT_STAGED, AUDIT_BRANCH)
                        for f in load_json(tdir / f"audit-{mode}.json", {}).get("leftovers", [])})
    if leftovers:
        lines += ["", "Leftovers (untracked, not committed; clean up): " + ", ".join(leftovers)]
    lines += ["", tested]
    print("\n".join(lines))
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """Where this branch's gated run stands: registration, attempts, stamps, last rounds."""
    repo = repo_root(args.repo)
    bdir = branch_dir(repo)
    idx, head = index_tree(repo), head_tree(repo)
    print(f"branch {branch_name(repo)}  state {bdir}")
    print(f"run: {json.dumps(run_info(repo))}")
    print(f"attempts: {json.dumps(load_json(bdir / 'attempts.json', {}))}")
    for label, tree in (("index", idx), ("HEAD", head)):
        tdir = tree_dir(repo, tree)
        have = [p.stem for p in sorted(tdir.glob("*.json"))] if tdir.exists() else []
        print(f"{label} tree {tree[:TREE_SHORT_LEN]}: {', '.join(have) or 'no stamps'}")
    for stage in (STAGE_PLAN, STAGE_REVIEW, TRIPWIRE_DIR, ADHOC_DIR):
        rounds = sorted((bdir / stage).glob("round-*"), key=lambda p: int(p.name.split("-")[1]))
        if rounds:
            last = rounds[-1]
            ingested = "ingested" if (last / "ingest.json").exists() else "awaiting outputs"
            print(f"{stage}: {len(rounds)} rounds, last {last.name} {ingested}")
    return EXIT_OK


def cmd_explain(args: argparse.Namespace) -> int:
    """Print tier, reviewers and checks for the staged change, a range, or a plan; free, writes nothing."""
    repo = repo_root(args.repo)
    cfg = load_config(repo)
    if args.plan:
        files, lines = plan_table_paths(Path(args.plan).read_text(encoding="utf-8")), None
        tier, reason = compute_tier(cfg, files, None, None)
    else:
        target = range_target(repo, args.range) if args.range else staged_target(repo, cfg, strict=False)
        files, lines = target.files, target.lines
        tier, reason = compute_tier(cfg, files, lines, lambda f: ast_neutral(repo, target.start, target.end, f))
    print(f"files: {len(files)}  lines: {lines if lines is not None else 'n/a (plan)'}")
    print(f"tier: {tier} ({reason})")
    stages = (STAGE_PLAN,) if args.plan else REVIEW_STAGES
    for r, p in pick_reviewers(cfg, all_reviewers(cfg, repo), reviewable(cfg, files), tier, stages):
        print(f"  reviewer {r.id:28} {'RUN ' if p.selected else 'skip'}  {p.reason}"
              + (f"  [{r.model_for(tier)}]" if p.selected else ""))
    if not args.plan:
        for c, p in pick_checks(cfg, files, tier):
            print(f"  check    {c.id:28} {'RUN ' if p.selected else 'skip'}  {p.reason}")
    return EXIT_OK


# --------------------------------------------------------------------------- calibration commands

PROMPT_POINTER = re.compile(r"Read `?([^`\s]+\.prompt\.md)`?")   # backticks optional: a bare path still matches


def transcript_report(path: Path) -> tuple[str | None, str | None, float | None]:
    """(prompt file it was pointed at, its final report, seconds) from a Claude Code subagent transcript.

    The report is the last message carrying a ```json block, else the last message: a reviewer that
    writes its full report and then hands back a short summary must not lose the block."""
    prompt = report = with_block = first = last = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("timestamp"):
            first, last = first or d["timestamp"], d["timestamp"]
        content = (d.get("message") or {}).get("content")
        if d.get("type") == "user" and prompt is None:
            text = content if isinstance(content, str) else " ".join(
                c.get("text", "") for c in content or [] if isinstance(c, dict))
            m = PROMPT_POINTER.search(text)
            prompt = m.group(1) if m else None
        if d.get("type") != "assistant" or not isinstance(content, list):
            continue
        for c in content:
            if c.get("type") == "tool_use" and c.get("name") == "SubagentHandback":
                report = str((c.get("input") or {}).get("message", ""))
            elif c.get("type") == "text" and c.get("text", "").strip():
                report = c["text"]
            else:
                continue
            if JSON_BLOCK.search(report):
                with_block = report
    seconds = None
    if first and last:
        seconds = round((datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds(), 1)
    return prompt, with_block or report, seconds


def cmd_capture(args: argparse.Namespace) -> int:
    """Save every reviewer's final report verbatim from its transcript, matched by the prompt file it was given."""
    rdir = Path(args.dir)
    meta = load_json(rdir / "dispatch.json", None)
    if meta is None:
        raise GateError(f"{rdir} has no dispatch.json")
    entries = list(meta["dispatch"]) + ([meta["canary"]] if meta.get("canary") else [])
    by_prompt = {p: e for e in entries for p in (e["prompt"], e.get("fallback_prompt")) if p}
    durations = load_json(rdir / "durations.json", {})
    for t in sorted(Path(args.transcripts).glob("*.output")):
        prompt, report, seconds = transcript_report(t)
        e = by_prompt.get(prompt or "")
        if e is None or not report or Path(e["out"]).exists():
            continue
        Path(e["out"]).write_text(report, encoding="utf-8")
        durations[e["name"]] = seconds
    save_json(rdir / "durations.json", durations)
    missing = [e["name"] for e in entries if not Path(e["out"]).exists()]
    print(f"captured {len(entries) - len(missing)}/{len(entries)}" + (f"; missing: {missing}" if missing else ""))
    return EXIT_OK if not missing else EXIT_FAIL


def cmd_calibrate_prepare(args: argparse.Namespace) -> int:
    """Write prompts that run each reviewer on its planted and clean fixtures."""
    repo = repo_root(args.repo)
    cfg = load_config(repo)
    wanted = set(args.ids or [])
    run_dir = CALIBRATION_RUNS / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    dispatch, missing = [], []
    for r in all_reviewers(cfg, repo):
        if wanted and r.id not in wanted:
            continue
        for variant in ("caught", "clean"):
            fx = fixture_dir(r, repo) / variant
            if not (fx / "expected.json").exists():
                missing.append(f"{r.id}/{variant}")
                continue
            ws, name = stage_fixture(fx, run_dir, r)
            entry = write_dispatch(run_dir, r, fixture_ctx(r, ws, "1"), r.model, name)
            dispatch.append(entry | {"variant": variant, "fixture": str(fx), "workspace": str(ws), "origin": r.origin,
                                     "sha": calibration_hash(r)})
    save_json(run_dir / "dispatch.json", {"repo": str(repo), "dispatch": dispatch, "missing": missing})
    print(json.dumps({"run_dir": str(run_dir), "dispatch": dispatch, "missing": missing}, indent=2))
    return EXIT_OK if not missing else EXIT_FAIL


def cmd_calibrate_score(args: argparse.Namespace) -> int:
    """Score a calibration run; record the hash of each reviewer that passed both fixtures."""
    run_dir = Path(args.run_dir)
    meta = load_json(run_dir / "dispatch.json", None)
    if meta is None:
        raise GateError(f"{run_dir} has no dispatch.json")
    repo = Path(meta["repo"])
    cfg = load_config(repo)
    by_id = {r.id: r for r in all_reviewers(cfg, repo)}
    durations = load_json(run_dir / "durations.json", {})
    scores: dict[str, dict[str, Any]] = {}
    for e in meta["dispatch"]:
        out = Path(e["out"])
        s = score_fixture(out.read_text(encoding="utf-8"), Path(e["fixture"]), e["id"], Path(e["workspace"])) \
            if out.exists() \
            else {"result": "invalid", "error": gate_view.NOT_SAVED_ERROR, "false_positives": 0}
        if e.get("sha") != calibration_hash(by_id[e["id"]]):     # edited after prepare: it ran other text
            s = {"result": "stale", "error": "reviewer edited since calibrate-prepare", "false_positives": 0}
        scores.setdefault(e["id"], {})[e["variant"]] = s | {"seconds": durations.get(e["name"]), "sha": e.get("sha")}
    all_ok = True
    for rid, s in sorted(scores.items()):
        ok = s.get("caught", {}).get("result") == "caught" and s.get("clean", {}).get("result") == "clean"
        all_ok = all_ok and ok
        print(f"{rid:28} caught: {s.get('caught', {}).get('result')}  clean: {s.get('clean', {}).get('result')}"
              f"  {'PASS' if ok else 'FAIL'}")
        if ok:
            r = by_id[rid]
            path = calibration_file(r, repo)
            record = load_json(path, {})
            # Branches can carry different texts of one project reviewer, so every text that passed stays valid.
            shas = calibrated_shas(r, repo) | {s["caught"]["sha"]}
            record[rid] = {"sha": s["caught"]["sha"], "shas": sorted(shas), "model": r.model, "at": now_iso(),
                           "false_positives": s["caught"]["false_positives"],
                           "seconds": {v: s[v].get("seconds") for v in ("caught", "clean")}}
            save_json(path, record)
    save_json(run_dir / "scores.json", scores)
    return EXIT_OK if all_ok else EXIT_FAIL


# --------------------------------------------------------------------------- housekeeping

def cmd_init(args: argparse.Namespace) -> int:
    """Register the branch's request (verbatim), plan and divergence log for prompts and hygiene."""
    repo = repo_root(args.repo)
    bdir = branch_dir(repo)
    bdir.mkdir(parents=True, exist_ok=True)
    run = run_info(repo)
    if args.request:
        shutil.copyfile(args.request, bdir / "request.md")
        run["request"] = str(bdir / "request.md")
    if args.plan:
        run["plan"] = str(Path(args.plan).resolve())
    if args.divergence_log:
        run["divergence_log"] = str(Path(args.divergence_log).resolve())
    run["at"] = now_iso()
    save_json(bdir / "run.json", run)
    print(json.dumps(run, indent=2))
    return EXIT_OK


def cmd_reset_attempts(args: argparse.Namespace) -> int:
    """Fresh attempts for a stage, after the user replied with direction."""
    repo = repo_root(args.repo)
    reset_attempt(repo, args.stage)
    print(f"{args.stage}: attempts reset")
    return EXIT_OK


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete tree and branch state older than PRUNE_DAYS."""
    repo = repo_root(args.repo)
    cutoff = time.time() - PRUNE_DAYS * 24 * 60 * 60
    removed = 0
    for parent in ("trees", "branches"):
        for p in (state_root(repo) / parent).glob("*"):
            if p.is_dir() and p.stat().st_mtime < cutoff:
                shutil.rmtree(p)
                removed += 1
    print(f"pruned {removed} state directories older than {PRUNE_DAYS} days")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """CLI: one subcommand per gate step."""
    ap = argparse.ArgumentParser(prog="gate.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name: str, func: Callable[[argparse.Namespace], int], help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--repo", help="the worktree (default: cwd)")
        p.set_defaults(func=func)
        return p

    p = add("init", cmd_init, "register request, plan and divergence log for this branch")
    p.add_argument("--request")
    p.add_argument("--plan")
    p.add_argument("--divergence-log")
    p = add("explain", cmd_explain, "print tier, reviewers and checks; free")
    p.add_argument("--range", help="A..B committed range instead of the staged change")
    p.add_argument("--plan", help="explain the plan gate for this plan file")
    p = add("review-prepare", cmd_review_prepare, "prepare a review round")
    p.add_argument("--tripwire", action="store_true")
    p.add_argument("--only", help="rules:<doc>[,<doc>] or checklist:<file>: one ad-hoc reviewer")
    p = add("review-ingest", cmd_review_ingest, "verify a review round")
    p.add_argument("--tripwire", action="store_true")
    p.add_argument("--only", help="the --only spec the round was prepared with")
    p.add_argument("--round", type=int)
    p = add("reject", cmd_reject, "reject one open finding with a counter-citation")
    p.add_argument("--finding", required=True, help="<reviewer>#<index>")
    p.add_argument("--cite", required=True, help="file:line that proves it wrong")
    p.add_argument("--reason", required=True)
    p.add_argument("--round", type=int)
    p.add_argument("--stage-dir", default=STAGE_REVIEW, choices=(STAGE_REVIEW, STAGE_PLAN, TRIPWIRE_DIR, ADHOC_DIR))
    p = add("note", cmd_note, "dashboard only: a plain-English line for a finding, and what was done about it")
    p.add_argument("--finding", required=True, help="<reviewer>#<index>")
    p.add_argument("--plain", help="what goes wrong, in easy English with no code names")
    p.add_argument("--done", help="what was done about it, e.g. 'Fixed: ...'")
    p.add_argument("--round", type=int)
    p.add_argument("--stage-dir", default=STAGE_REVIEW, choices=(STAGE_REVIEW, STAGE_PLAN))
    p = add("handoff", cmd_handoff, "write HANDOFF.md so a session in the worktree's own window can continue")
    p.add_argument("--note", help="what the next session should know that the gate state does not say")
    p = add("plan-prepare", cmd_plan_prepare, "prepare a plan-gate round")
    p.add_argument("--plan", required=True)
    p = add("plan-ingest", cmd_plan_ingest, "verify a plan-gate round")
    p.add_argument("--round", type=int)
    p = add("checks-run", cmd_checks_run, "run the selected checks")
    p.add_argument("--baseline", help="re-run this failing check on a clean base worktree")
    p = add("audit", cmd_audit, "commit hygiene (staged) or pre-push audit (branch)")
    p.add_argument("--mode", choices=(AUDIT_STAGED, AUDIT_BRANCH), required=True)
    p.add_argument("--fetch", action="store_true", help="fetch the base before the freshness check")
    p.add_argument("--no-freshness", action="store_true")
    p.add_argument("--guard", action="store_true", help="the hook's re-audit: no attempt checked or counted")
    p = add("stamp", cmd_stamp, "write a stage's stamp")
    p.add_argument("--stage", choices=(STAGE_PLAN, *PUSH_STAGES), required=True)
    p.add_argument("--plan")
    p.add_argument("--accept", action="store_true", help="consume the user's #accept-gate token")
    p.add_argument("--approve", action="store_true", help="plan only: consume the user's #approve-plan token")
    p = add("summary", cmd_summary, "MR summary, or --tested for the session's test table and divergences")
    p.add_argument("--tested", action="store_true")
    p.add_argument("--head", action="store_true", help="use HEAD's tree instead of the index")
    add("status", cmd_status, "where this branch's run stands")
    p = add("calibrate-prepare", cmd_calibrate_prepare, "prompts for running reviewers on their fixtures")
    p.add_argument("ids", nargs="*")
    p = add("calibrate-score", cmd_calibrate_score, "score a calibration run and record passing reviewers")
    p.add_argument("--run-dir", required=True)
    p = add("capture", cmd_capture, "Claude Code: save reviewer reports verbatim from their transcripts")
    p.add_argument("--dir", required=True, help="the round or calibration run directory (has dispatch.json)")
    p.add_argument("--transcripts", required=True, help="the session's tasks directory (from an Agent output_file)")
    p = add("reset-attempts", cmd_reset_attempts, "fresh attempts for a stage")
    p.add_argument("--stage", choices=tuple(CAP_KEYS), required=True)
    add("prune", cmd_prune, f"delete state older than {PRUNE_DAYS} days")
    return ap


def refresh_view(repo_arg: str | None) -> Path | None:
    """Rewrite the branch's dashboard and status-line view (gate_view.py); never fails the command."""
    try:
        repo = repo_root(repo_arg)
        page = gate_view.write(branch_dir(repo), view_caps(load_config(repo)), view_trees(repo), repo)
        if (repo / HANDOFF_FILE).exists():          # once written, kept current after every command
            write_handoff(repo)
        return page
    except Exception:  # a view is a convenience: a broken one must not break a gate step
        return None


def view_trees(repo: Path) -> dict[str, str] | None:
    """The index and HEAD trees, for the dashboard's stamps; None when git can't say (a conflict, no commit)."""
    try:
        return {TREE_INDEX: index_tree(repo), TREE_HEAD: head_tree(repo)}
    except GateError:
        return None


def dashboard_line(page: Path) -> str:
    """The dashboard's path for the user, plus the Windows path a host browser opens under WSL."""
    distro = os.environ.get("WSL_DISTRO_NAME")
    windows = f"  (Windows: \\\\wsl.localhost\\{distro}{str(page).replace('/', chr(92))})" if distro else ""
    return f"dashboard (reloads itself): {page}{windows}"


def main(argv: list[str] | None = None) -> int:
    """Entry point: run one subcommand, map GateError to its exit code, then refresh the dashboard."""
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GateError as exc:
        print(f"gate: {exc}", file=sys.stderr)
        if exc.code == EXIT_CAP:                    # the run stops for the user: leave a way to resume it elsewhere
            try:
                announce_handoff(repo_root(getattr(args, "repo", None)))
            except Exception:  # the handoff is a convenience: the cap message above is what matters
                pass
        return exc.code
    finally:
        page = refresh_view(getattr(args, "repo", None))
        if page is not None:                        # stderr: stdout is pasted verbatim (summary --tested)
            print(dashboard_line(page), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
