"""Read-only views of one branch's gate run: `view.json`, an auto-refreshing dashboard, a status line.

gate.py calls `write()` after every command, so `<branch state>/dashboard.html`
(open it in a browser; it reloads itself) and `view.json` always show where the
run stands. An agent's status line (Claude Code's, for one) runs
`python3 gate_view.py statusline`, which reads the newest `view.json` of the
repository it is in. Nothing here changes the gate's decisions; it only reads
what gate.py wrote.

Python 3.10-compatible, stdlib only: the status line runs under /usr/bin/python3.
"""
from __future__ import annotations

import ast
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate_common import (AUDIT_BRANCH, AUDIT_STAGED, BLOCKING, CANARY_OK, PASSING, SKIP_WAIVES, STAGE_CHECKS, STAGE_PLAN,  # noqa: E402
                         STAGE_PUSH, STAGE_REVIEW, STATE_DIR, STATUS_FAIL, TREE_HEAD, TREE_INDEX, USER_SKIP_FILE,
                         VERDICT_PASS, repo_state)

VIEW_FILE, DASHBOARD_FILE, NOTES_FILE, REJECTIONS_FILE = "view.json", "dashboard.html", "notes.json", "rejections.json"
OLDER_DECIDED_FILE = "older-decided.json"
"""The older bugs the user chose to leave, by finding key, so they are not asked about twice."""
STAGES = ("plan", "review")
COMMIT = "commit"
"""The commit-hygiene audit of the staged code; it has no tries of its own."""
STAGE_TITLES = {"plan": "plan review", "review": "code review", "checks": "checks", "push": "push audit",
                COMMIT: "commit hygiene"}
"""What each stage reviews: the plan before any code, then the implemented code."""
STAGE_LINE_TITLES = {"review": "AI code review", "checks": "Automatic tests", "push": "Final check before pushing"}
"""What the top box calls each stage, for someone who has never seen the gate."""
TREE_STAGES = (STAGE_REVIEW, STAGE_CHECKS, STAGE_PUSH)
"""Stages stamped per tree; the plan is stamped per plan text."""
SEVERITIES = ("blocker", "should-fix", "nit")
OPEN_STATES = ("open", "reraised")
REFRESH_S = 5
"""How often the dashboard reloads itself."""
SHORT_TREE = 10
STOP_LINE_CHARS = 140
"""The top box trims each reason to this, so the reader never has to open a section to see why it stopped."""
MISSED_FILES_ERROR = "did not report reading"
"""How gate.py's ingest starts the error of a report that skipped some of its files."""
NOT_SAVED_ERROR = "output not saved"
"""How gate.py's ingest starts the error of a report that was never saved."""
WRONG_TREE_ERROR = "tree "
"""How gate.py's ingest starts the error of a report on a different version of the code."""
DIVERGENCE_COLUMNS = ("n", "tag", "plan", "did", "why")
"""A divergence log's table columns: `| # | tag | plan said | did instead | why / alternative rejected |`."""


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _round(rdir: Path, rejections: dict[str, Any]) -> dict[str, Any]:
    """One round: every finding (with any `gate.py note` on it, and the agent's reason for a rejected one),
    the open ones, each reviewer's status and error, or who it is waiting for."""
    ingest = _load(rdir / "ingest.json")
    if ingest is None:
        dispatch = _load(rdir / "dispatch.json") or {}
        names = [e.get("name", e.get("id", "?")) for e in dispatch.get("dispatch", [])]
        return {"name": rdir.name, "status": "running", "reviewers": names, "counts": {}, "open": [], "items": []}
    notes = _load(rdir / NOTES_FILE) or {}
    counts = {s: 0 for s in SEVERITIES}
    items, reviewers = [], []
    for result in ingest.get("results", []):
        mine = {s: 0 for s in SEVERITIES}
        for i, f in enumerate(result.get("findings", [])):
            sev = f.get("severity", "nit")
            counts[sev] = counts.get(sev, 0) + 1
            mine[sev] = mine.get(sev, 0) + 1
            if f.get("state") == "dropped":           # its quote was not in the file: never a real finding
                continue
            ref = f"{result.get('id')}#{i}"           # what `gate.py note` and `reject` call it
            note = notes.get(ref, {})
            items.append({"round": rdir.name, "reviewer": result.get("id"), "id": f.get("id"), "ref": ref,
                          "severity": sev, "state": f.get("state"), "file": f.get("file"), "line": f.get("line"),
                          "quote": f.get("quote", ""), "why": f.get("why", ""), "fix": f.get("fix", ""),
                          "plain": note.get("plain") or f.get("plain", ""), "done": note.get("done", ""),
                          "rejected": rejections.get(f.get("key")) if f.get("state") == "rejected" else None,
                          "older": bool(f.get("older")), "key": f.get("key")})
        reviewers.append({"id": result.get("id"), "status": result.get("status"), "error": result.get("error", ""),
                          "counts": mine})
    open_items = [f for f in items if f["state"] in OPEN_STATES and f["severity"] in BLOCKING and not f["older"]]
    return {"name": rdir.name, "status": "ingested", "canary": (ingest.get("canary") or {}).get("result"),
            "seconds": ingest.get("seconds"), "counts": counts, "reviewers": reviewers, "open": open_items,
            "items": items}


def _rounds(stage_dir: Path) -> list[dict[str, Any]]:
    dirs = sorted((d for d in stage_dir.glob("round-*") if d.is_dir()), key=lambda d: int(d.name.split("-")[1]))
    rejections = _load(stage_dir / REJECTIONS_FILE) or {}
    return [_round(d, rejections) for d in dirs]


def _plan_stamp(bdir: Path, plan: str | None) -> str | None:
    """The verdict of the plan stamp for the registered plan's exact text (stamps are keyed by its sha256), if any."""
    try:
        sha = hashlib.sha256(Path(plan).read_bytes()).hexdigest() if plan else None
    except OSError:
        return None
    stamp = _load(bdir.parent.parent / "plans" / f"{sha}.json") if sha else None
    return stamp.get("verdict") if stamp else None


def divergences(log: str | None) -> list[dict[str, str]] | None:
    """The rows of a divergence log's table, or None when no log is registered or its file is gone.

    Every table row after the header counts, so rows appended later show too; a `|` inside the last
    cell stays part of it.
    """
    try:
        text = Path(log).read_text(encoding="utf-8") if log else None
    except OSError:
        text = None
    if text is None:
        return None
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip("|"))]
        if cells[0] == "#" or not any(cells):        # the header row, or an empty one
            continue
        last = len(DIVERGENCE_COLUMNS) - 1
        cells = cells[:last] + [""] * (last - len(cells)) + [" | ".join(cells[last:])]
        rows.append(dict(zip(DIVERGENCE_COLUMNS, cells)))
    return rows


def _tree_state(bdir: Path, tree: str | None) -> dict[str, Any]:
    """One tree's stamp verdicts and commit-hygiene hits, from `<repo state>/trees/<tree>`."""
    if not tree:
        return {"tree": None, "stamps": {}, "hits": {}, "waived": []}
    tdir = bdir.parent.parent / "trees" / tree
    stamps = {s: (_load(tdir / f"{s}.json") or {}).get("verdict") for s in TREE_STAGES}
    hits = {mode: (_load(tdir / f"audit-{mode}.json") or {}).get("hits", []) for mode in (AUDIT_STAGED, AUDIT_BRANCH)}
    waived = SKIP_WAIVES.get((_load(tdir / USER_SKIP_FILE) or {}).get("kind"), ())
    return {"tree": tree, "stamps": {s: v for s, v in stamps.items() if v}, "hits": hits, "waived": list(waived)}


def _quote_gone(f: dict[str, Any], path: Path | None) -> bool:
    """Whether a finding's quoted text has left its file, or the file is gone. Not when it can't be read: it stays listed."""
    if path is None or not f["quote"]:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return " ".join(f["quote"].split()) not in " ".join(text.split())


def _set_aside(stages: dict[str, Any], root: Path | None) -> list[dict[str, Any]]:
    """Every finding the agent rejected instead of fixing, once each.

    `gone` says a code-review finding's quoted text has left its file. A plan finding never reads as gone: the
    handoff moves the plan, so its path says nothing about whether the plan changed.
    """
    found: dict[tuple[Any, ...], dict[str, Any]] = {}
    for stage in STAGES:
        for r in stages.get(stage) or []:
            for f in r["items"]:
                if f["state"] == "rejected":
                    path = root / f["file"] if root and stage == STAGE_REVIEW else None
                    found.setdefault((f["reviewer"], f["id"], f["file"], f["quote"]),
                                     f | {"stage": stage, "found_in": f"{r['name']} of the {STAGE_TITLES[stage]}",
                                          "gone": _quote_gone(f, path)})
    return list(found.values())


def _older_bugs(stages: dict[str, Any], root: Path | None, decided: list[str]) -> list[dict[str, Any]]:
    """Real defects a reviewer says this change did not cause, still in the code and not yet decided on, once each.

    A re-review re-checks them, so one that a later valid round of the same reviewer no longer reports is fixed."""
    found: dict[Any, dict[str, Any]] = {}
    for r in stages.get(STAGE_REVIEW) or []:
        if r["status"] == "running":
            continue
        reported = {f["key"] for f in r["items"] if f["older"] and f["state"] in OPEN_STATES}
        valid = {x["id"] for x in r["reviewers"] if x["status"] != "invalid"}
        found = {k: f for k, f in found.items() if k in reported or f["reviewer"] not in valid}
        for f in r["items"]:
            if f["older"] and f["state"] in OPEN_STATES and f["severity"] in BLOCKING and f["key"] not in decided:
                found.setdefault(f["key"], f)
    return [f for f in found.values() if not _quote_gone(f, root / f["file"] if root else None)]


def summary(bdir: Path, caps: dict[str, int], trees: dict[str, str] | None = None,
            root: Path | None = None) -> dict[str, Any]:
    """Everything the dashboard and the status line show, from one branch's state folder.

    `trees` is {"index": <tree>, "head": <tree>} when the caller knows them (gate.py does); the top box then
    says which stamps that code has. `root` is the repository, to tell whether a set-aside finding is still there.
    """
    run = _load(bdir / "run.json") or {}
    stages = {stage: _rounds(bdir / stage) for stage in STAGES}
    checks = _load(bdir / "checks" / "state.json") or {}
    request = _load_text(Path(run["request"])) if run.get("request") else ""
    return {
        "branch": bdir.name.replace("__", "/"), "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "request": request, "plan": run.get("plan"),
        "divergence_log": run.get("divergence_log"), "divergences": divergences(run.get("divergence_log")),
        "attempts": _load(bdir / "attempts.json") or {}, "caps": caps,
        "plan_stamp": _plan_stamp(bdir, run.get("plan")), "stages": stages, "set_aside": _set_aside(stages, root),
        "older": _older_bugs(stages, root, _load(bdir / STAGE_REVIEW / OLDER_DECIDED_FILE) or []),
        "checks": {cid: {"status": c.get("status"), "seconds": c.get("seconds"), "tree": c.get("tree"),
                         "log": c.get("log"), "failed_ids": c.get("failed_ids") or []} for cid, c in checks.items()},
        "trees": {k: _tree_state(bdir, t) for k, t in (trees or {}).items()},
        "dashboard": str(bdir / DASHBOARD_FILE),
    }


def _load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def status_line(view: dict[str, Any]) -> str:
    """One line: branch, the current stage and round, attempts, open findings, checks."""
    parts = [f"gate {view['branch']}"]
    stage, rounds = _current(view)
    if rounds:
        last = rounds[-1]
        used, cap = view["attempts"].get(stage, 0), view["caps"].get(stage)
        parts.append(f"{STAGE_TITLES[stage]} {last['name'].replace('round-', 'r')}"
                     + (f" · attempts {used}/{cap}" if cap else ""))
        if last["status"] == "running":
            parts.append(f"reviewing ({len(last['reviewers'])})")
        elif last["open"]:
            sevs = [f["severity"] for f in last["open"]]
            parts.append(f"open {sevs.count('blocker')} blocker, {sevs.count('should-fix')} should-fix")
        elif _blocked(last):
            parts.append(f"not passed: {len(_invalid(last))} invalid reports"
                         + ("" if _canary_ok(last) else ", canary missed"))
        else:
            serious = len(_by_problem(_serious_set_aside(view)))
            plan = len(_by_problem(_plan_set_aside(view)))
            older = len(_by_problem(_pending_older(view)))
            parts.append("clean" + (f", {serious} set aside, not fixed" if serious else "")
                         + (f", {_count(plan, 'plan finding')} set aside" if plan else "")
                         + (f", {_count(older, 'older bug')} for you to decide" if older else ""))
    elif view.get("plan_stamp"):
        parts.append(f"plan {view['plan_stamp']}")
    checks = view.get("checks") or {}
    if checks:
        bad = [cid for cid, c in checks.items() if c["status"] == STATUS_FAIL]
        trees = sorted({c["tree"][:SHORT_TREE] for c in checks.values() if c["tree"]})
        on = f" @{','.join(trees)}" if trees else ""      # the code version the checks last ran on
        parts.append(f"checks fail: {','.join(bad)}{on}" if bad else f"checks pass ({len(checks)}){on}")
    return " · ".join(parts)


# --------------------------------------------------------------------------- dashboard

_CSS = """
:root { --bg:#fff; --fg:#1c1f24; --muted:#5f6672; --line:#d9dde3; --panel:#f6f7f9; --ask:#fff8e6;
        --critical:#b42318; --important:#b54708; --nice:#175cd3; --ok:#157f3b; }
@media (prefers-color-scheme: dark) { :root { --bg:#15171b; --fg:#e6e8ec; --muted:#9aa2ae; --line:#333843;
        --panel:#1d2026; --ask:#2a2412; --critical:#ff8a7a; --important:#fdb022; --nice:#84adff; --ok:#6fd393; } }
body { background:var(--bg); color:var(--fg); font:15px/1.55 system-ui,sans-serif; margin:0 auto; max-width:900px; padding:16px; }
h1 { font-size:20px; margin:0 0 4px; } .muted { color:var(--muted); } code, pre { font:12px ui-monospace,monospace; }
pre { background:var(--panel); border:1px solid var(--line); padding:8px; white-space:pre-wrap; }
table { border-collapse:collapse; width:100%; } th, td { border:1px solid var(--line); padding:4px 8px; text-align:left; }
.ask { background:var(--ask); border:2px solid var(--important); border-radius:10px; padding:12px 16px; margin:16px 0; }
.ask.none { background:var(--panel); border-color:var(--line); }
.ask h2 { font-size:17px; margin:0 0 6px; } .ask li { margin:4px 0; } .ask b { font-family:ui-monospace,monospace; }
details.section { border:1px solid var(--line); border-radius:10px; margin:20px 0; background:var(--panel); }
details.section > summary { font-size:17px; font-weight:600; padding:12px 16px; cursor:pointer; }
details.section > .body { padding:4px 16px 12px; background:var(--bg); border-top:1px solid var(--line); border-radius:0 0 10px 10px; }
details.card { border:1px solid var(--line); border-left:5px solid var(--line); border-radius:6px; margin:8px 0; }
details.card > summary { padding:8px 12px; cursor:pointer; } details.card > .body { padding:0 12px 10px 28px; }
details.card.critical { border-left-color:var(--critical); } details.card.important { border-left-color:var(--important); }
details.card.nice { border-left-color:var(--nice); }
.badge { display:inline-block; min-width:92px; text-align:center; border-radius:10px; padding:0 8px; margin-right:8px;
         font-size:12px; font-weight:700; color:#fff; }
.badge.critical { background:var(--critical); } .badge.important { background:var(--important); }
.badge.nice { background:var(--nice); } @media (prefers-color-scheme: dark) { .badge { color:#15171b; } }
.count { font-weight:600; margin-left:6px; } .count.critical { color:var(--critical); }
.count.important { color:var(--important); } .count.nice { color:var(--nice); }
.ask details.why summary { cursor:pointer; color:var(--muted); } .ask details.why li { font-size:13px; }
dl.div { display:grid; grid-template-columns:max-content 1fr; gap:4px 12px; margin:6px 0; }
dl.div dt { font-weight:600; color:var(--muted); } dl.div dd { margin:0; }
.pass { color:var(--ok); } .fail { color:var(--critical); } .tech { font-size:13px; color:var(--muted); }
"""

RATINGS = {"blocker": ("critical", "Critical"), "should-fix": ("important", "Important"), "nit": ("nice", "Nice to have")}
"""Each reviewer severity as the dashboard shows it: a CSS class and a word."""

_KEEP_STATE_JS = """
<script>
const KEY = "gate-dashboard:" + location.pathname;
try {
  const saved = JSON.parse(localStorage.getItem(KEY) || "{}");
  for (const id of saved.open || []) { const d = document.getElementById(id); if (d) d.open = true; }
  if (saved.y) window.scrollTo(0, saved.y);
} catch (e) {}
setTimeout(() => {
  try {
    const open = [...document.querySelectorAll("details[open]")].map(d => d.id).filter(Boolean);
    localStorage.setItem(KEY, JSON.stringify({open, y: window.scrollY}));
  } catch (e) {}
  location.reload();
}, %d);
</script>
"""
"""Reloads the page and keeps what the reader opened and where they had scrolled to."""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _current(view: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """The stage the run is in now (code review once it has started) and its rounds."""
    stage = "review" if view["stages"].get("review") else "plan"
    return stage, view["stages"].get(stage) or []


def _minutes(seconds: Any) -> str:
    return f"about {max(1, round(seconds / 60))} minutes" if isinstance(seconds, (int, float)) else "a few minutes"


def _canary_ok(last: dict[str, Any]) -> bool:
    return last.get("canary") in CANARY_OK or last.get("canary") is None


def _invalid(last: dict[str, Any]) -> list[dict[str, Any]]:
    """The reviewers of a round whose report the gate could not use."""
    return [r for r in last.get("reviewers", []) if isinstance(r, dict) and r.get("status") == "invalid"]


def _blocked(last: dict[str, Any]) -> bool:
    """Whether an ingested round failed: an open blocking finding, an invalid report, or a missed canary."""
    return last["status"] != "running" and bool(last["open"] or _invalid(last) or not _canary_ok(last))


def trim_cell(text: str, limit: int) -> str:
    """`text` cut at a word boundary to about `limit` characters, any code span it cut open closed again."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut + ("`" if cut.count("`") % 2 else "") + " …"


def _plain_error(error: str) -> str:
    """Why the gate could not use a reviewer's report, in plain words."""
    if error.startswith(MISSED_FILES_ERROR):
        try:
            n = len(ast.literal_eval(error[len(MISSED_FILES_ERROR):].strip()))
        except (ValueError, SyntaxError):
            return "did not read all of its files to the end"
        return f"did not read {n} of its files to the end"
    if error.startswith(NOT_SAVED_ERROR):
        return "its report was not saved"
    if error.startswith(WRONG_TREE_ERROR):
        return "it reviewed a different version of the code"
    return f"its report could not be read ({trim_cell(error, STOP_LINE_CHARS)})"


CODE, NOT_CODE = "a problem in the code", "not a code problem"


def _li(who: str, said: str, kind: str, state: str) -> str:
    """One reason a stage did not pass: who, what it said, whose problem it is, and whether it is handled."""
    return f"<li><b>{_e(who)}</b> &mdash; {_e(said)} <i>({kind}{'; ' + state if state else ''})</i></li>"


def _review_reasons(last: dict[str, Any]) -> list[str]:
    """Why a review round did not pass: a missed canary, invalid reports, blocking findings."""
    lines = [] if _canary_ok(last) else [_li("canary", "the reviewers missed the planted test bug, so the round "
                                             "proved nothing", NOT_CODE, "")]
    lines += [_li(r["id"], _plain_error(r.get("error", "")), NOT_CODE, "")
              for r in _invalid(last)]
    for f in last["items"]:
        if f["severity"] not in BLOCKING or f.get("older") or f["state"] not in OPEN_STATES + ("rejected",):
            continue
        state = ("set aside by the agent, not fixed; no longer blocks" if f["state"] == "rejected"
                 else "handled, waiting for a reviewer to re-check it" if f["done"] else "still open")
        word = RATINGS.get(f["severity"], RATINGS["nit"])[1].lower()
        lines.append(_li(f["reviewer"], f"{word} {f['id']}: {trim_cell(f['plain'] or f['why'], STOP_LINE_CHARS)}",
                         CODE, state))
    return lines


def _check_reasons(view: dict[str, Any]) -> list[str]:
    """Every red check, with its log and the last test it named as failing."""
    lines = []
    for cid, c in (view.get("checks") or {}).items():
        if c["status"] != STATUS_FAIL:
            continue
        last_test = f"last failing test <code>{_e(c['failed_ids'][-1])}</code>, " if c.get("failed_ids") else ""
        lines.append(f"<li><b>{_e(cid)}</b> &mdash; red on tree <code>{_e((c['tree'] or '')[:SHORT_TREE])}</code>: "
                     f"{last_test}log <code>{_e(c['log'])}</code> <i>({CODE}; still open)</i></li>")
    return lines


def _audit_reasons(view: dict[str, Any], key: str, mode: str) -> list[str]:
    """The commit-hygiene hits of the last audit of the index (`staged`) or HEAD (`branch`) tree."""
    hits = (((view.get("trees") or {}).get(key) or {}).get("hits") or {}).get(mode, [])
    return [_li(h.get("rule", ""), f"{h.get('file', '')}: {h.get('detail', '')}",
                "a problem in what is being committed", "still open") for h in hits]


def _passed(view: dict[str, Any], stage: str) -> bool:
    """Whether `stage` holds for the code it applies to: a passing stamp, or the user's skip token waived it."""
    trees = view.get("trees") or {}
    on = (trees.get(TREE_HEAD) if stage == STAGE_PUSH else trees.get(TREE_INDEX)) or {}
    return (on.get("stamps") or {}).get(stage) in PASSING or stage in (on.get("waived") or [])


def _all_clean(view: dict[str, Any]) -> bool:
    """Whether review and checks earned a PASS on the current files, not an acceptance or a skip."""
    stamps = ((view.get("trees") or {}).get(TREE_INDEX) or {}).get("stamps") or {}
    return all(stamps.get(s) == VERDICT_PASS for s in (STAGE_REVIEW, STAGE_CHECKS))


def _failing(view: dict[str, Any]) -> dict[str, list[str]]:
    """Each stage that has not passed, with its reasons: the plan or code review, the checks, the push audit."""
    out: dict[str, list[str]] = {}
    plan_rounds, review_rounds = view["stages"].get(STAGE_PLAN) or [], view["stages"].get(STAGE_REVIEW) or []
    if plan_rounds and not review_rounds and view.get("plan_stamp") not in PASSING and _blocked(plan_rounds[-1]):
        out[STAGE_PLAN] = _review_reasons(plan_rounds[-1])
    if review_rounds and _blocked(review_rounds[-1]) and not _passed(view, STAGE_REVIEW):
        out[STAGE_REVIEW] = _review_reasons(review_rounds[-1])
    for stage, lines in ((STAGE_CHECKS, _check_reasons(view)), (COMMIT, _audit_reasons(view, TREE_INDEX, AUDIT_STAGED)),
                         (STAGE_PUSH, _audit_reasons(view, TREE_HEAD, AUDIT_BRANCH))):
        if lines and not _passed(view, stage):
            out[stage] = lines
    for stage in (STAGE_CHECKS, STAGE_PUSH):
        if _at_cap(view, stage) and stage not in out and not _passed(view, stage):
            out[stage] = [_li(STAGE_TITLES[stage], f"failed all {view['caps'][stage]} of its tries; run `gate.py "
                              f"status` for the last result", CODE, "still open")]
    return out


def _at_cap(view: dict[str, Any], stage: str) -> bool:
    cap = view["caps"].get(stage)
    return bool(cap) and view["attempts"].get(stage, 0) >= cap


def _stage_lines(view: dict[str, Any]) -> str:
    """One line per stage, in plain words: whether it passed on the code it applies to, or has not run yet."""
    trees = view.get("trees") or {}
    index, head = trees.get(TREE_INDEX) or {}, trees.get(TREE_HEAD) or {}
    plan = view.get("plan_stamp")
    rows = [f"<li>Plan review: {_e(plan.lower()) if plan else 'not done for this version of the plan (a commit does not need it)'}</li>"]
    for stage in TREE_STAGES:
        on = head if stage == STAGE_PUSH else index           # a push sends HEAD; a commit records the index
        verdict = (on.get("stamps") or {}).get(stage)
        what = "the last commit" if stage == STAGE_PUSH else "the current files"
        version = f" on {what} (version <code>{_e((on.get('tree') or '')[:SHORT_TREE])}</code>)" if on.get("tree") else ""
        result = (("passed" if verdict == VERDICT_PASS else _e(verdict.lower())) + version if verdict
                  else "skipped by you" if stage in (on.get("waived") or []) else "not run yet")
        rows.append(f"<li>{STAGE_LINE_TITLES[stage]}: {result}</li>")
    return f"<ul>{''.join(rows)}</ul>"


def _passed_headline(view: dict[str, Any]) -> str:
    """The OK box's headline: ready to commit, committed, or nothing in the way yet."""
    trees = view.get("trees") or {}
    index, head = trees.get(TREE_INDEX) or {}, trees.get(TREE_HEAD) or {}
    if not all(_passed(view, s) for s in (STAGE_REVIEW, STAGE_CHECKS)):
        return "Nothing is blocking so far &mdash; not ready to commit yet"
    word = "All passed" if _all_clean(view) else "You accepted or skipped a check"
    return f"{word} &mdash; {'committed' if index.get('tree') == head.get('tree') else 'ready to commit'}"


def _count(n: int, thing: str) -> str:
    return f"{n} {thing}" if n == 1 else f"{n} {thing}s"


def _what_happened(view: dict[str, Any], failing: dict[str, list[str]]) -> str:
    """Why it did not pass, for someone who has never seen the gate: no reviewer ids, no rule ids."""
    out = []
    for stage, subject, next_step in ((STAGE_PLAN, "plan", "built"), (STAGE_REVIEW, "code", "committed")):
        if stage not in failing:
            continue
        last = view["stages"][stage][-1]
        invalid, serious = _invalid(last), last["open"]
        if not _canary_ok(last):
            out.append("The AI reviewers missed a test bug planted to check them, so this review can't be trusted.")
        if invalid:
            out.append(f"AI reviewers check the {subject} before it can be {next_step}. "
                       f"{len(invalid)} of them did not finish, so the check is not complete.")
        if serious:
            fixed = sum(1 for f in serious if f["done"])
            out.append(f"The review found {_count(len(serious), 'serious problem')} in the {subject}."
                       + (f" The agent has fixed {fixed}; a reviewer still has to confirm." if fixed else ""))
        elif invalid:
            out.append(f"A report that did not finish is not a problem with your {subject}. Anything serious still open "
                       "is listed below.")
    if STAGE_CHECKS in failing:
        red = sum(1 for c in (view.get("checks") or {}).values() if c["status"] == STATUS_FAIL)
        out.append(f"{_count(red, 'automatic test')} failed." if red else "The automatic tests failed.")
    if COMMIT in failing:
        out.append(f"{_count(len(failing[COMMIT]), 'commit rule')} failed.")
    if STAGE_PUSH in failing:
        out.append(f"{_count(len(failing[STAGE_PUSH]), 'push rule')} failed.")
    return " ".join(out)


def _run_again(view: dict[str, Any], failing: dict[str, list[str]]) -> str:
    """What typing `run again` does, in plain words."""
    what = []
    for stage in (STAGE_PLAN, STAGE_REVIEW):
        if stage not in failing:
            continue
        last = view["stages"][stage][-1]
        invalid, serious = _invalid(last), last["open"]
        unread = [r for r in invalid if r.get("error", "").startswith(MISSED_FILES_ERROR)]
        if unread:
            who = "That reviewer tries" if len(unread) == 1 else "Those reviewers try"
            # Only `review-prepare` moves a reviewer up a model tier; `plan-prepare` sends it at the same one.
            what.append(f"{who} again, on a stronger AI model when there is one, so it can get through the big files."
                        if stage == STAGE_REVIEW else f"{who} again.")
        if len(unread) < len(invalid):
            what.append("A reviewer whose report could not be used runs again.")
        if any(not f["done"] for f in serious):
            what.append("The agent fixes what is left, then the review runs again.")
        elif serious:
            what.append("A reviewer checks the agent's fixes.")
        if not invalid and not serious:
            what.append("The review runs again.")
        what.append(f"Takes {_minutes(last.get('seconds'))}.")
    if STAGE_CHECKS in failing:
        what.append("The agent fixes the failure, then the tests run again.")
    if COMMIT in failing or STAGE_PUSH in failing:
        what.append("The agent fixes this, then the rule runs again.")
    return " ".join(what)


def _replies(view: dict[str, Any], failing: dict[str, list[str]]) -> str:
    """The replies the user can type, the recommended one first."""
    accept = ("<li><b>#approve-plan</b> &mdash; accept the plan as it is and start building.</li>"
              if list(failing) == [STAGE_PLAN] else
              "<li><b>#accept-gate</b> &mdash; skip this check and go on. What it found stays on record.</li>")
    return (f"<ul><li><b>run again</b> (recommended) &mdash; {_run_again(view, failing)}</li>{accept}"
            f"<li><b>#abort</b> &mdash; stop here. Nothing changes.</li></ul>")


def _serious_set_aside(view: dict[str, Any]) -> list[dict[str, Any]]:
    """The blocking code-review findings that the agent set aside and that are still in the code."""
    return [f for f in view.get("set_aside") or []
            if f["stage"] == STAGE_REVIEW and f["severity"] in BLOCKING and not f["gone"]]


def _plan_set_aside(view: dict[str, Any]) -> list[dict[str, Any]]:
    """The blocking plan findings that the agent set aside: they never stop the box, but the wording owns up."""
    return [f for f in view.get("set_aside") or [] if f["stage"] == STAGE_PLAN and f["severity"] in BLOCKING]


def _pending_older(view: dict[str, Any]) -> list[dict[str, Any]]:
    """The older bugs still waiting for the user; a review that already holds for this code asks nothing."""
    return [] if _passed(view, STAGE_REVIEW) else view.get("older") or []


def _by_problem(findings: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Findings grouped by their plain sentence: a reviewer raises one per line, so one problem can be several."""
    problems: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        problems.setdefault(f["plain"] or f["why"], []).append(f)
    return problems


def _set_aside_box(view: dict[str, Any], serious: list[dict[str, Any]]) -> str:
    """The top box when the rest passed but the agent set serious findings aside: that call is the user's."""
    trees = view.get("trees") or {}
    tree = (trees.get(TREE_INDEX) or {}).get("tree")
    stamped = all(_passed(view, s) for s in (STAGE_REVIEW, STAGE_CHECKS))
    lead = ("Committed, but the agent" if stamped and tree and tree == (trees.get(TREE_HEAD) or {}).get("tree")
            else "Review and tests passed, but the agent" if _all_clean(view)
            else "You accepted or skipped a check, and the agent" if stamped else "The agent")
    problems = _by_problem(serious)
    items = "".join(f"<li>{_e(p)}{f' <i>(in {len(fs)} places)</i>' if len(fs) > 1 else ''}</li>"
                    for p, fs in problems.items())
    return (f"<div class=ask><h2>{lead} did not fix {_count(len(problems), 'serious problem')}</h2>"
            f"<p><strong>What happened:</strong> The agent set these aside instead of fixing them. Its reasons are in "
            f"section 5.{'' if stamped else ' The remaining checks are still to run.'}</p>"
            f"<ul>{items}</ul><p><strong>What to do:</strong> type one of these in the chat.</p>"
            f"<ul><li><b>fix them</b> (recommended) &mdash; the agent fixes them, and a reviewer checks the fix.</li>"
            f"<li><b>leave them</b> &mdash; go on without fixing them. They stay listed here until they are fixed.</li>"
            f"</ul>{_stage_lines(view)}</div>")


def _older_box(view: dict[str, Any], older: list[dict[str, Any]]) -> str:
    """The top box when the review found bugs this change did not cause: whether to fix them is the user's call."""
    problems = _by_problem(older)
    items = "".join(f"<li>{_e(p)}</li>" for p in problems)
    return (f"<div class=ask><h2>Your decision: {_count(len(problems), 'older bug')} found</h2>"
            f"<p><strong>What happened:</strong> The reviewers found problems that were already in the code. This change "
            f"did not cause them or make them worse.</p><ul>{items}</ul>"
            f"<p><strong>What to do:</strong> type one of these in the chat.</p>"
            f"<ul><li><b>fix them</b> (recommended) &mdash; the agent fixes them as part of this work.</li>"
            f"<li><b>#accept-gate</b> &mdash; leave them for now and go on. They stay on record.</li>"
            f"<li><b>#abort</b> &mdash; stop here. Nothing changes.</li></ul>{_stage_lines(view)}</div>")


def _decision(view: dict[str, Any]) -> str:
    """The top box: everything passed, or what happened and what to do, readable with no knowledge of the gate."""
    stage, rounds = _current(view)
    if rounds and rounds[-1]["status"] == "running":
        return (f"<div class='ask none'><h2>Running: the {STAGE_TITLES[stage]}</h2><p>{len(rounds[-1]['reviewers'])} "
                f"reviewers are reading the work.</p>{_stage_lines(view)}</div>")
    failing = _failing(view)
    if not failing and _pending_older(view):
        return _older_box(view, _pending_older(view))
    if not failing:
        serious = _serious_set_aside(view)
        return (_set_aside_box(view, serious) if serious
                else f"<div class='ask none'><h2>{_passed_headline(view)}</h2>{_stage_lines(view)}</div>")
    details = "".join(f"<ul>{''.join(lines)}</ul>" for lines in failing.values())
    details = f"<details class=why id=stop-details><summary>Details</summary>{details}</details>"
    happened = f"<p><strong>What happened:</strong> {_what_happened(view, failing)}</p>"
    subject = ("Building has not started" if list(failing) == [STAGE_PLAN]
               else "Not pushed" if list(failing) == [STAGE_PUSH] else "Not committed")
    if any(_at_cap(view, s) for s in failing):
        return (f"<div class=ask><h2>{subject}: your choice is needed</h2>{happened}"
                f"<p><strong>What to do:</strong> type one of these in the chat.</p>{_replies(view, failing)}"
                f"{details}</div>")
    waiting = ("If the agent has stopped and is waiting for you, type <b>#approve-plan</b> to accept the plan as it "
               "is. Otherwise nothing is needed from you." if list(failing) == [STAGE_PLAN]
               else "Nothing is needed from you.")
    return f"<div class=ask><h2>{subject} yet: the agent is fixing this</h2>{happened}<p>{waiting}</p>{details}</div>"


def _card(f: dict[str, Any], status: str, sid: str) -> str:
    """One issue, folded: its rating and the problem in plain English; open it for what was done and the detail."""
    css, word = RATINGS.get(f["severity"], RATINGS["nit"])
    done = f"<p><b>What the agent did:</b> {_e(f['done'])}</p>" if f["done"] else ""
    # The id carries the section, so no two cards on the page share one and the reload reopens the right card.
    return (f"<details class='card {css}' id='{sid}-{_e(f['round'])}-{_e(f['ref'])}'><summary>"
            f"<span class='badge {css}'>{word}</span>{_e(f['plain'] or f['why'])}</summary><div class=body>"
            f"<p class=muted>{status}</p>{done}<div class=tech><p>Reviewer's technical note ({_e(f['reviewer'])} "
            f"{_e(f['id'])}, {_e(f['round'])}): <code>{_e(f['file'])}:{_e(f['line'])}</code></p>"
            f"<p>{_e(f['why'])}</p><p><i>Suggested fix:</i> {_e(f['fix'])}</p></div></div></details>")


def _counts(cards: list[tuple[dict[str, Any], str]]) -> str:
    return "".join(f"<span class='count {css}'>{sum(1 for f, _ in cards if f['severity'] == sev)} {word.lower()}</span>"
                   for sev, (css, word) in RATINGS.items() if any(f["severity"] == sev for f, _ in cards))


def _cards(cards: list[tuple[dict[str, Any], str]], sid: str) -> str:
    """The cards, most serious first."""
    order = list(RATINGS)
    ranked = sorted(cards, key=lambda c: order.index(c[0]["severity"]) if c[0]["severity"] in order else len(order))
    return "".join(_card(f, status, sid) for f, status in ranked) if cards else "<p class=muted>Nothing here.</p>"


def _section(sid: str, title: str, intro: str, cards: list[tuple[dict[str, Any], str]], extra: str = "") -> str:
    """A folded section: its title with how many of each rating it holds, then the cards and any `extra`."""
    return (f"<details class=section id={sid}><summary>{title} <span class=muted>({len(cards)})</span>{_counts(cards)}"
            f"</summary><div class=body><p class=muted>{intro}</p>{_cards(cards, sid)}{extra}</div></details>")


def _three_sections(view: dict[str, Any]) -> str:
    """1. what the plan review changed, 2. code fixes for the user to look at, 3. what is still left to do."""
    plan_rounds = [r for r in view["stages"].get("plan") or [] if r["status"] != "running"]
    code_rounds = [r for r in view["stages"].get("review") or [] if r["status"] != "running"]
    plan = [(f, f"Found in {r['name']}. " + ("Plan changed." if f["done"] else "Changed in the plan before it was approved."))
            for r in plan_rounds for f in r["items"] if f["state"] != "rejected"]   # section 5 lists the rejected ones
    fixed, earlier, left = [], [], []
    pending_older = {f.get("key") for f in view.get("older") or []}
    set_aside = {f.get("key") for f in view.get("set_aside") or []} - pending_older
    for k, r in enumerate(code_rounds):
        reported_later = {g["key"] for x in code_rounds[k + 1:] for g in x["items"]}
        ran_later = {x["id"] for y in code_rounds[k + 1:] for x in y["reviewers"] if x["status"] != "invalid"}
        for f in r["items"]:
            found = f"Found in {r['name']}. "
            later = f["reviewer"] in ran_later               # an unusable report re-checked nothing
            settled = f["key"] in set_aside and (f.get("older") or later)
            if f["state"] == "rejected" or f["key"] in reported_later or settled:   # section 5 or the later copy lists it; once is enough
                continue
            if f.get("older") and f["severity"] in BLOCKING:
                (left if f["key"] in pending_older else earlier).append((f, found + (
                    "An older bug this change did not cause: your decision." if f["key"] in pending_older
                    else "An older bug: fixed since, or you chose to leave it.")))
            elif f["done"] and not later and f["severity"] not in BLOCKING and not f["done"].lower().startswith("fixed"):
                left.append((f, found + "Nice to have; the agent left it as it is."))
            elif f["done"]:
                (earlier if later else fixed).append((f, found + ("A later round re-checked the code." if later
                                                                  else "Waiting for a reviewer to confirm the fix.")))
            elif later and f["severity"] in BLOCKING:
                earlier.append((f, found + "Fixed before the next round, which re-checked the code."))
            elif later:
                earlier.append((f, found + "Nice to have; whether it was fixed was not recorded."))
            else:
                left.append((f, found + "Not handled yet."))
    older = (f"<details class=section id=earlier><summary>Earlier rounds "
             f"<span class=muted>({len(earlier)})</span>{_counts(earlier)}</summary><div class=body>"
             f"<p class=muted>Reviewer wording only.</p>{_cards(earlier, 'earlier')}</div></details>" if earlier else "")
    return (_section("plan", "1 &middot; Changes to the original plan",
                     "What the plan reviewer found before any code was written, and how the plan changed.", plan)
            + _section("fixed", "2 &middot; Fixes made to the code &mdash; for your review",
                       "Problems the last code review found that the agent has fixed since. No reviewer has checked "
                       "these fixes yet.", fixed, older)
            + _section("left", "3 &middot; Found, not handled yet",
                       "Problems the last code review found that nobody has fixed yet. Nice-to-have ones never block.",
                       left))


def _rejected(view: dict[str, Any]) -> str:
    """5. Every finding the agent set aside instead of fixing, in any round of either stage, with its reason."""
    findings = view.get("set_aside") or []
    fixed_since = [f"<li>{_e(p)}</li>" for p in _by_problem([f for f in findings if f["gone"]])]
    cards = []
    for fs in _by_problem([f for f in findings if not f["gone"]]).values():
        reasons = []
        for f in fs:
            why = f.get("rejected") or {}
            reasons.append(f"The agent's reason: {_e(why['reason'])} (cites <code>{_e(why.get('cite'))}</code>)."
                           if why.get("reason") else "The agent's reason is on record with the gate.")
        also = ", ".join(f"<code>{_e(f['file'])}:{_e(f['line'])}</code>" for f in fs[1:])
        cards.append((fs[0], f"Found in {_e(fs[0]['found_in'])}; set aside by the agent. "
                             + " ".join(dict.fromkeys(reasons)) + (f" The same problem is also at {also}." if also else "")))
    extra = (f"<h3>Set aside earlier, fixed since</h3><p class=muted>The text they complained about is no longer in "
             f"the code.</p><ul>{''.join(fixed_since)}</ul>" if fixed_since else "")
    return _section("rejected", "5 &middot; Findings the agent did not fix",
                    "The agent set these aside instead of fixing them, so they no longer block. Open one for its "
                    "reason. A reviewer that raises one again blocks until it is fixed.", cards, extra)


def _md(text: str) -> str:
    """A log cell as HTML: escaped, with its `backticked` spans as code."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", _e(text))


DIVERGENCE_TAGS = {"LOGIC": "important", "MECH": "nice"}
"""A divergence-log tag and the rating colour its row is drawn in; any other tag is drawn as MECH."""
DIVERGENCE_SUMMARY_CHARS = 140
"""A divergence row's folded line shows its "did instead" cell cut to this many characters."""


def _divergences(view: dict[str, Any]) -> str:
    """4. Every row of the divergence log: what the work did instead of the plan, and why."""
    rows = view.get("divergences")
    if rows is None and view.get("divergence_log"):
        body = f"<p class=muted>The divergence log file is missing: <code>{_e(view['divergence_log'])}</code>.</p>"
    elif rows is None:
        body = "<p class=muted>No divergence log registered (<code>gate.py init --divergence-log</code>).</p>"
    elif not rows:
        body = f"<p class=muted>None recorded in <code>{_e(view.get('divergence_log'))}</code>.</p>"
    else:
        cards = "".join(
            f"<details class='card {DIVERGENCE_TAGS.get(r['tag'].strip('[]'), 'nice')}' id='divergence-{_e(r['n'])}'>"
            f"<summary><span class='badge {DIVERGENCE_TAGS.get(r['tag'].strip('[]'), 'nice')}'>#{_e(r['n'])} "
            f"{_e(r['tag'].strip('[]'))}</span>{_md(trim_cell(r['did'], DIVERGENCE_SUMMARY_CHARS))}</summary>"
            f"<div class=body><dl class=div><dt>Plan said</dt><dd>{_md(r['plan'])}</dd><dt>Did instead</dt>"
            f"<dd>{_md(r['did'])}</dd><dt>Why</dt><dd>{_md(r['why'])}</dd></dl></div></details>" for r in rows)
        body = (f"<p class=muted>Where the work differs from the approved plan, as the agent logged it in "
                f"<code>{_e(view.get('divergence_log'))}</code>. The fidelity reviewer and the commit check H1 let a "
                f"change through when it is logged here, so this is what to read. LOGIC rows change behaviour; "
                f"MECH rows are mechanical. Open a row for what the plan said and why.</p>{cards}")
    return (f"<details class=section id=divergences><summary>4 &middot; Divergences from the plan "
            f"<span class=muted>({len(rows or [])})</span></summary><div class=body>{body}</div></details>")


def _more(view: dict[str, Any]) -> str:
    checks = view.get("checks") or {}
    rows = "".join(f"<tr><td>{_e(cid)}</td><td class='{_e(c['status'])}'>{_e(c['status'])}</td><td>{_e(c['seconds'])}s</td>"
                   f"<td><code>{_e((c['tree'] or '')[:SHORT_TREE])}</code></td></tr>" for cid, c in checks.items())
    table = (f"<table><tr><th>check</th><th>result</th><th>time</th><th>code version</th></tr>{rows}</table>"
             if checks else "<p class=muted>No checks have run yet.</p>")
    return (f"<details class=section id=more><summary>More: test checks and your request</summary><div class=body>"
            f"<h3>Test checks</h3>{table}<h3>Your request</h3><pre>{_e(view['request'])}</pre></div></details>")


def render(view: dict[str, Any]) -> str:
    """The dashboard page; it reloads itself every REFRESH_S seconds and keeps what the reader opened."""
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width, initial-scale=1'><title>gate · {_e(view['branch'])}</title>"
            f"<style>{_CSS}</style></head><body><h1>gate · {_e(view['branch'])}</h1>"
            f"<p class=muted>{_e(status_line(view))} · updated {_e(view['updated'])} · reloads every {REFRESH_S}s</p>"
            + _decision(view) + _three_sections(view) + _divergences(view) + _rejected(view) + _more(view)
            + _KEEP_STATE_JS % (REFRESH_S * 1000) + "</body></html>")


# --------------------------------------------------------------------------- handoff

def windows_path(path: str) -> str | None:
    """The same file as a Windows Explorer path when this runs under WSL, else None."""
    distro = os.environ.get("WSL_DISTRO_NAME")
    return f"\\\\wsl.localhost\\{distro}{path}".replace("/", "\\") if distro and path.startswith("/") else None


def next_step(view: dict[str, Any], plan: str) -> str:
    """What a fresh session should do first, from where the gates stand."""
    stage, rounds = _current(view)
    if view.get("plan_stamp") not in PASSING:
        if any(r["status"] != "running" for r in view["stages"].get("plan") or []):
            return (f"The plan changed since it was reviewed. Show the user the change and wait for their "
                    f"`#approve-plan`; then `gate.py stamp --repo . --stage plan --plan {plan} --approve`.")
        return f"Finish the plan gate: `/gate plan {plan}`, then show the user the verdict and wait for their OK."
    if stage == "plan":
        return (f"Implement the approved plan: invoke the `implement-plan` skill with `plan: {plan}`, "
                f"`reviewer: gate`, `owns: commit, integration`.")
    last = rounds[-1]
    if last["status"] == "running":
        return (f"A code review round ({last['name']}) was running when this was written. Run `gate.py status`; "
                f"if its reviewers never reported, run `/gate review` again.")
    used, cap = view["attempts"].get(stage, 0), view["caps"].get(stage)
    if _blocked(last) and cap and used >= cap:
        return ("Stop and ask the user. The code review used all its tries; the dashboard's decision box shows the "
                "state. Their replies: \"run again\" (then `gate.py reset-attempts --repo . --stage review` and "
                "`/gate review`), `#accept-gate` (then `gate.py stamp --repo . --stage review --accept`), or `#abort`.")
    if last["open"]:
        return f"Fix the open findings of {last['name']} (dashboard section 3), then run `/gate review` again."
    if _blocked(last):
        return f"{last['name']} did not pass (an invalid report or a missed canary): run `/gate review` again."
    if _pending_older(view):
        return ("Stop and ask the user: the dashboard's decision box lists older bugs this change did not cause. Their "
                "replies: `fix them`, `#accept-gate` (then `gate.py stamp --repo . --stage review --accept`), or `#abort`.")
    return ("The code review is clean. Run `/gate checks` unless they already passed on the staged code, then propose "
            "the commit message and wait for the user's OK.")


def handoff(view: dict[str, Any], repo: Path, files: dict[str, str], note: str = "") -> str:
    """HANDOFF.md: everything a fresh session in this worktree needs to pick the run up, from the gate state."""
    stage, rounds = _current(view)
    win = windows_path(view["dashboard"])
    lines = [f"# Handoff: {view['branch']}", "",
             "Written by `gate.py`, and rewritten after every gate command, so it is always current. "
             "Open a session on this folder and run `/resume-handoff`.", "",
             "## Where", "",
             f"- Worktree: `{repo}`, branch `{view['branch']}`.",
             "- Python: this worktree's own `.venv/bin/python`, never a bare `python`.",
             f"- Dashboard (reloads itself): `{view['dashboard']}`" + (f"; from Windows: `{win}`" if win else "") + ".",
             "", "## Files", ""]
    labels = {"request": "Your request, verbatim", "plan": "The plan (edit this copy only)",
              "divergence_log": "Divergence log (where the work differs from the plan)"}
    lines += [f"- {labels[k]}: `{v}`" for k, v in files.items()]
    stamp = view.get("plan_stamp") or "not approved yet"
    lines += ["", "## Where it stands", "", f"- Plan review: {stamp}."]
    if stage == "review" and rounds:
        last = rounds[-1]
        used, cap = view["attempts"].get(stage, 0), view["caps"].get(stage)
        handled = sum(1 for f in last["open"] if f["done"])
        lines.append(f"- Code review: {len(rounds)} rounds, tries used {used}/{cap}; {len(last['open'])} blocking "
                     f"issues open after {last['name']}, {handled} of them handled since.")
    checks = view.get("checks") or {}
    if checks:
        bad = [cid for cid, c in checks.items() if c["status"] == STATUS_FAIL]
        trees = sorted({c["tree"][:SHORT_TREE] for c in checks.values() if c["tree"]})
        on = f" on code version {', '.join(trees)}" if trees else ""
        lines.append(f"- Checks: {'failing: ' + ', '.join(bad) if bad else 'passed'}{on}.")
    lines += ["", "## Next step", "", next_step(view, files.get("plan", "<plan>"))]
    if note:
        lines += ["", "## Notes from the last session", "", note]
    lines += ["", "## Rules", "",
              "- No `git commit`, push, tag or MR without the user's explicit OK in this session, unless their "
              "`/resume-handoff` message says `gated`. Commits go through `/gate review` and `/gate checks`.",
              "- `#skip-*`, `#accept-gate` and `#approve-plan` are the user's to type. Never type or arm them, and never "
              "recommend one; list them only as the user's replies when a stage stops.",
              "- Read the project's rules file (`AGENTS.md` or `CLAUDE.md`) before editing; it holds the commit identity and message style.", ""]
    return "\n".join(lines)


def write(bdir: Path, caps: dict[str, int], trees: dict[str, str] | None = None, root: Path | None = None) -> Path:
    """Refresh `view.json` and `dashboard.html` for one branch; returns the dashboard's path."""
    view = summary(bdir, caps, trees, root)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / VIEW_FILE).write_text(json.dumps(view, indent=1), encoding="utf-8")
    page = bdir / DASHBOARD_FILE
    page.write_text(render(view), encoding="utf-8")
    return page


def newest_view(cwd: str) -> dict[str, Any] | None:
    """The most recently updated branch view of the repository `cwd` is in (any of its worktrees)."""
    try:
        common = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    views = list((repo_state(common, STATE_DIR) / "branches").glob(f"*/{VIEW_FILE}"))
    if not views:
        return None
    return _load(max(views, key=lambda p: p.stat().st_mtime))


def _statusline_main() -> int:
    """Status line in Claude Code's format: JSON with the cwd on stdin, one line out (nothing outside a gate run)."""
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or "."
    view = newest_view(cwd)
    if view:
        print(status_line(view))
    return 0


if __name__ == "__main__":
    sys.exit(_statusline_main() if sys.argv[1:] == ["statusline"] else 2)
