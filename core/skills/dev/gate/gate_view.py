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

import hashlib
import html
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate_common import PASSING, STATE_DIR, repo_state  # noqa: E402

VIEW_FILE, DASHBOARD_FILE, NOTES_FILE = "view.json", "dashboard.html", "notes.json"
STAGES = ("plan", "review")
STAGE_TITLES = {"plan": "plan review", "review": "code review"}
"""What each stage reviews: the plan before any code, then the implemented code."""
SEVERITIES = ("blocker", "should-fix", "nit")
OPEN_STATES = ("open", "reraised")
REFRESH_S = 5
"""How often the dashboard reloads itself."""
SHORT_TREE = 10


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _round(rdir: Path) -> dict[str, Any]:
    """One round: every finding (with any `gate.py note` on it), the open ones, or who it is waiting for."""
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
                          "why": f.get("why", ""), "fix": f.get("fix", ""),
                          "plain": note.get("plain") or f.get("plain", ""), "done": note.get("done", "")})
        reviewers.append({"id": result.get("id"), "status": result.get("status"), "counts": mine})
    open_items = [f for f in items if f["state"] in OPEN_STATES and f["severity"] != "nit"]
    return {"name": rdir.name, "status": "ingested", "canary": (ingest.get("canary") or {}).get("result"),
            "seconds": ingest.get("seconds"), "counts": counts, "reviewers": reviewers, "open": open_items,
            "items": items}


def _rounds(stage_dir: Path) -> list[dict[str, Any]]:
    dirs = sorted((d for d in stage_dir.glob("round-*") if d.is_dir()), key=lambda d: int(d.name.split("-")[1]))
    return [_round(d) for d in dirs]


def _plan_stamp(bdir: Path, plan: str | None) -> str | None:
    """The verdict of the plan stamp for the registered plan's exact text (stamps are keyed by its sha256), if any."""
    try:
        sha = hashlib.sha256(Path(plan).read_bytes()).hexdigest() if plan else None
    except OSError:
        return None
    stamp = _load(bdir.parent.parent / "plans" / f"{sha}.json") if sha else None
    return stamp.get("verdict") if stamp else None


def summary(bdir: Path, caps: dict[str, int]) -> dict[str, Any]:
    """Everything the dashboard and the status line show, from one branch's state folder."""
    run = _load(bdir / "run.json") or {}
    stages = {stage: _rounds(bdir / stage) for stage in STAGES}
    checks = _load(bdir / "checks" / "state.json") or {}
    request = _load_text(Path(run["request"])) if run.get("request") else ""
    return {
        "branch": bdir.name.replace("__", "/"), "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "request": request, "plan": run.get("plan"),
        "attempts": _load(bdir / "attempts.json") or {}, "caps": caps,
        "plan_stamp": _plan_stamp(bdir, run.get("plan")), "stages": stages,
        "checks": {cid: {"status": c.get("status"), "seconds": c.get("seconds"), "tree": c.get("tree"),
                         "log": c.get("log")} for cid, c in checks.items()},
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
        else:
            parts.append("clean")
    elif view.get("plan_stamp"):
        parts.append(f"plan {view['plan_stamp']}")
    checks = view.get("checks") or {}
    if checks:
        bad = [cid for cid, c in checks.items() if c["status"] != "pass"]
        trees = sorted({(c["tree"] or "")[:SHORT_TREE] for c in checks.values()})
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


def _decision(view: dict[str, Any]) -> str:
    """The top box: whether the user has to decide anything now, and the replies they can type."""
    stage, rounds = _current(view)
    title = STAGE_TITLES[stage]
    if not rounds:
        return "<div class='ask none'><h2>Nothing for you to decide</h2><p>No review has run yet.</p></div>"
    last = rounds[-1]
    if last["status"] == "running":
        return (f"<div class='ask none'><h2>Nothing for you to decide yet</h2><p>The {title} is running: "
                f"{len(last['reviewers'])} reviewers are reading the work.</p></div>")
    if not last["open"]:
        return f"<div class='ask none'><h2>Nothing for you to decide</h2><p>The {title} found no open issues.</p></div>"
    used, cap = view["attempts"].get(stage, 0), view["caps"].get(stage)
    n = len(last["open"])
    if not cap or used < cap:
        return (f"<div class='ask none'><h2>Nothing for you to decide yet</h2><p>The {title} found {n} issues. "
                f"The agent is fixing them; the next round checks the fixes.</p></div>")
    handled = sum(1 for f in last["open"] if f["done"])
    state = (f"The agent has handled all {n} of them, but no reviewer has checked those fixes yet."
             if handled == n else f"The agent has handled {handled} of them; see section 3 for the rest.")
    options = [f"<li><b>review again</b> &mdash; a reviewer checks the fixes ({_minutes(last.get('seconds'))}).</li>"]
    if stage == "plan":
        options.append("<li><b>#approve-plan</b> &mdash; approve the plan as it is now and start building.</li>")
    else:
        options.append("<li><b>#accept-gate</b> &mdash; go on without checking the fixes again. "
                       "The issues stay on record.</li>")
    options.append("<li><b>#abort</b> &mdash; stop the work here.</li>")
    return (f"<div class=ask><h2>You need to decide</h2><p>The {title} has used all {cap} of its tries; its last "
            f"round found {n} issues that block it. {state}</p><p>Type one of these in the chat:</p>"
            f"<ul>{''.join(options)}</ul></div>")


def _card(f: dict[str, Any], status: str) -> str:
    """One issue, folded: its rating and the problem in plain English; open it for what was done and the detail."""
    css, word = RATINGS.get(f["severity"], RATINGS["nit"])
    done = f"<p><b>What the agent did:</b> {_e(f['done'])}</p>" if f["done"] else ""
    return (f"<details class='card {css}' id='{_e(f['round'])}-{_e(f['ref'])}'><summary>"
            f"<span class='badge {css}'>{word}</span>{_e(f['plain'] or f['why'])}</summary><div class=body>"
            f"<p class=muted>{status}</p>{done}<div class=tech><p>Reviewer's technical note ({_e(f['reviewer'])} "
            f"{_e(f['id'])}, {_e(f['round'])}): <code>{_e(f['file'])}:{_e(f['line'])}</code></p>"
            f"<p>{_e(f['why'])}</p><p><i>Suggested fix:</i> {_e(f['fix'])}</p></div></div></details>")


def _counts(cards: list[tuple[dict[str, Any], str]]) -> str:
    return "".join(f"<span class='count {css}'>{sum(1 for f, _ in cards if f['severity'] == sev)} {word.lower()}</span>"
                   for sev, (css, word) in RATINGS.items() if any(f["severity"] == sev for f, _ in cards))


def _cards(cards: list[tuple[dict[str, Any], str]]) -> str:
    """The cards, most serious first."""
    order = list(RATINGS)
    ranked = sorted(cards, key=lambda c: order.index(c[0]["severity"]) if c[0]["severity"] in order else len(order))
    return "".join(_card(f, status) for f, status in ranked) if cards else "<p class=muted>Nothing here.</p>"


def _section(sid: str, title: str, intro: str, cards: list[tuple[dict[str, Any], str]], extra: str = "") -> str:
    """A folded section: its title with how many of each rating it holds, then the cards and any `extra`."""
    return (f"<details class=section id={sid}><summary>{title} <span class=muted>({len(cards)})</span>{_counts(cards)}"
            f"</summary><div class=body><p class=muted>{intro}</p>{_cards(cards)}{extra}</div></details>")


def _three_sections(view: dict[str, Any]) -> str:
    """1. what the plan review changed, 2. code fixes for the user to look at, 3. what is still left to do."""
    plan_rounds = [r for r in view["stages"].get("plan") or [] if r["status"] != "running"]
    code_rounds = [r for r in view["stages"].get("review") or [] if r["status"] != "running"]
    plan = [(f, f"Found in {r['name']}. " + ("Plan changed." if f["done"] else "Changed in the plan before it was approved."))
            for r in plan_rounds for f in r["items"]]
    fixed, earlier, left = [], [], []
    for k, r in enumerate(code_rounds):
        later = k < len(code_rounds) - 1
        for f in r["items"]:
            found = f"Found in {r['name']}. "
            if f["state"] == "rejected":
                (earlier if later else fixed).append((f, found + "The agent showed the finding was wrong; the reason "
                                                              "is on record with the gate."))
            elif f["done"]:
                (earlier if later else fixed).append((f, found + ("A later round re-checked the code." if later
                                                                  else "Waiting for a reviewer to confirm the fix.")))
            elif later and f["severity"] != "nit":
                earlier.append((f, found + "Fixed before the next round, which re-checked the code."))
            elif later:
                earlier.append((f, found + "Nice to have; whether it was fixed was not recorded."))
            else:
                left.append((f, found + "Not handled yet."))
    older = (f"<details class=section id=earlier><summary>Earlier rounds, already re-checked by a later review "
             f"<span class=muted>({len(earlier)})</span>{_counts(earlier)}</summary><div class=body>"
             f"<p class=muted>Reviewer wording only.</p>{_cards(earlier)}</div></details>" if earlier else "")
    return (_section("plan", "1 &middot; Changes to the original plan",
                     "What the plan reviewer found before any code was written, and how the plan changed.", plan)
            + _section("fixed", "2 &middot; Fixes made to the code &mdash; for your review",
                       "Problems the last code review found that the agent has fixed since. No reviewer has checked "
                       "these fixes yet.", fixed, older)
            + _section("left", "3 &middot; Found, not handled yet",
                       "Problems the last code review found that nobody has fixed yet. Nice-to-have ones never block.",
                       left))


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
            + _decision(view) + _three_sections(view) + _more(view)
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
    if last["open"] and cap and used >= cap:
        return ("Stop and ask the user. The code review used all its tries; the dashboard's decision box shows the "
                "state. Their replies: \"review again\" (then `gate.py reset-attempts --repo . --stage review` and "
                "`/gate review`), `#accept-gate` (then `gate.py stamp --repo . --stage review --accept`), or `#abort`.")
    if last["open"]:
        return f"Fix the open findings of {last['name']} (dashboard section 3), then run `/gate review` again."
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
        bad = [cid for cid, c in checks.items() if c["status"] != "pass"]
        trees = sorted({(c["tree"] or "")[:SHORT_TREE] for c in checks.values()})
        lines.append(f"- Checks: {'failing: ' + ', '.join(bad) if bad else 'passed'} on code version {', '.join(trees)}.")
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


def write(bdir: Path, caps: dict[str, int]) -> Path:
    """Refresh `view.json` and `dashboard.html` for one branch; returns the dashboard's path."""
    view = summary(bdir, caps)
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
