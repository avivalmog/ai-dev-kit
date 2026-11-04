#!/usr/bin/env python3
"""PreToolUse guard for the gates: commits and pushes need stamps; wildcard staging and stamp tampering are blocked.

Registered in ~/.claude/settings.json for Bash|Write|Edit|NotebookEdit. Exit 2 blocks the tool call and
shows stderr to the agent; exit 0 allows it. Only the agent's tool calls pass through here: the user's own
terminal is never gated. The flow it enforces: ~/.claude/DEV_FLOW.md.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
GATE_DIR = HOME / ".claude" / "skills" / "gate"
sys.path.insert(0, str(GATE_DIR))
try:
    from gate_common import (AUDIT_BRANCH, AUDIT_STAGED, COMMIT_STAGES, PASSING, PUSH_STAGES, SKIP_WAIVES,
                             STATE_DIR, STATE_DIR_NAME, TOKEN_ACCEPT, TOKEN_APPROVE_PLAN, TOKEN_SKIP_GATES, TOKEN_SKIP_REVIEW, TOKEN_TTL_S,
                             TOKENS_DIR, TREE_SHORT_LEN, USER_SKIP_FILE, tree_state)
    BASH_PROTECTED = re.compile(re.escape(STATE_DIR_NAME) + r"/(?:tokens|\S*\.json)")
    IMPORT_ERROR = None
except ImportError as exc:          # fail closed below: gated git commands are refused, nothing else is touched
    IMPORT_ERROR = exc

GATE_CMD = ["uv", "run", "--no-project", "--python", "3.12", str(GATE_DIR / "gate.py")]
AUDIT_TIMEOUT_S = 180
FILE_TOOLS = ("Write", "Edit", "NotebookEdit")
SEPARATOR_CHARS = set(";&|()")       # a token made only of these ends a simple command (`;(` and `|&` included)
HEREDOC = re.compile(r"<<-?[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z0-9_.-]+))")
REDIRECTIONS = {">", ">>", "<", ">&", "&>", "<&", ">|", "&>>", "<<", "<<<"}
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
COMMIT_VALUE_OPTS = {"--message", "--file", "--reuse-message", "--reedit-message", "--author", "--date", "--fixup",
                     "--squash", "--template", "--cleanup", "--trailer"}
COMMIT_VALUE_SHORT = set("mFcCt")
COMMIT_GLUED_VALUE_SHORT = set("uS")         # `-uno`, `-S<keyid>`: an optional value, only ever glued on
COMMIT_TREE_CHANGING_LONG = {"--all", "--include", "--only", "--interactive", "--patch", "--pathspec-from-file",
                             "--pathspec-file-nul"}
COMMIT_TREE_CHANGING_SHORT = set("aiop")
GATED_GIT = re.compile(r"\b(?:git\b[^|;&]*\b(?:commit|push|add)|glab\s+mr\s+create|gh\s+pr\s+create)\b")
SHELL_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time"}
GATED_SUBCOMMANDS = {"commit", "push", "add"}
INDEX_CHANGING = {"add", "rm", "mv", "restore", "reset", "update-index", "stash", "checkout", "switch", "apply",
                  "read-tree", "merge", "cherry-pick", "revert", "pull", "am", "rebase"}   # the tree a commit records
GATE_ENGINE = "gate.py"
ADD_WILDCARDS = {"-A", "--all", ".", "-u", "--update", ":/", "*", "--no-ignore-removal"}
PUSH_FREE = {"--delete", "-d", "--dry-run", "-n"}
MR_COMMANDS = (("glab", "mr", "create"), ("gh", "pr", "create"))


def block(message: str) -> None:
    """Refuse the tool call with a reason the agent can act on."""
    sys.stderr.write(f"BLOCKED by the gate guard: {message}\n"
                     "Gates: ~/.claude/DEV_FLOW.md. Only the user can skip (#skip-review / #skip-gates).\n")
    sys.exit(2)


def protected_path(path: str) -> bool:
    """Gate state JSON (stamps, rounds, attempts) and user tokens are written only by gate.py and the user's tags."""
    p = os.path.abspath(os.path.expanduser(path))
    return p.startswith(str(STATE_DIR) + "/") and (p.endswith(".json") or p.startswith(str(TOKENS_DIR) + "/"))


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run git in `repo`."""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def skip_heredoc(command: str, i: int, delim: str) -> int:
    """Index just past the line that ends a heredoc body starting at `i`; ValueError when it never ends."""
    while i < len(command):
        j = command.find("\n", i)
        line, i = (command[i:], len(command)) if j < 0 else (command[i:j], j + 1)
        if line.strip() == delim:
            return i
    raise ValueError(f"heredoc {delim!r} never ends")


def logical_lines(command: str) -> list[str]:
    """The lines the shell runs: continuations joined, comments and heredoc bodies dropped.

    A newline inside quotes stays in its line. Raises ValueError on an unterminated quote or heredoc,
    so an unreadable command fails closed instead of hiding a git step on a later line.
    """
    lines: list[str] = []
    cur: list[str] = []
    heredocs: list[str] = []
    quote, i, n = None, 0, len(command)
    while i < n:
        ch = command[i]
        if quote == "'":                              # no escapes inside single quotes
            quote = None if ch == "'" else quote
            cur.append(ch)
            i += 1
        elif ch == "\\" and i + 1 < n:
            if command[i + 1] != "\n":                 # backslash-newline is a continuation: drop both
                cur.append(command[i:i + 2])
            i += 2
        elif quote == '"':
            quote = None if ch == '"' else quote
            cur.append(ch)
            i += 1
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
            i += 1
        elif ch == "#" and (not cur or cur[-1] in " \t;&|()"):
            while i < n and command[i] != "\n":        # a comment: up to the newline, which still ends the line
                i += 1
        elif command.startswith("<<<", i):            # a here-string, not a heredoc
            cur.append("<<<")
            i += 3
        elif command.startswith("<<", i) and (m := HEREDOC.match(command, i)):
            heredocs.append(next(g for g in m.groups() if g))
            cur.append(m.group(0))
            i = m.end()
        elif ch == "\n":
            lines.append("".join(cur))
            cur = []
            i += 1
            for delim in heredocs:
                i = skip_heredoc(command, i, delim)
            heredocs = []
        else:
            cur.append(ch)
            i += 1
    if quote:
        raise ValueError("unterminated quote")
    if heredocs:
        raise ValueError("heredoc with no body")
    lines.append("".join(cur))
    return [line for line in lines if line.strip()]


def segments(command: str) -> list[list[str]]:
    """Shell command split into simple commands, line by line; raises ValueError on unparseable quoting."""
    out = []
    for line in logical_lines(command):
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""                      # logical_lines already dropped the comments
        current: list[str] = []
        for tok in lexer:
            if tok and set(tok) <= SEPARATOR_CHARS:     # `""` lexes to an empty token: an argument, not a separator
                if current:
                    out.append(drop_redirections(current))
                current = []
            else:
                current.append(tok)
        if current:
            out.append(drop_redirections(current))
    return out


def drop_redirections(tokens: list[str]) -> list[str]:
    """Remove `2>&1`, `> file` and the like, so a redirection is never read as a pathspec."""
    out: list[str] = []
    skip_target = False
    for tok in tokens:
        if skip_target:
            skip_target = False
            continue
        if tok in REDIRECTIONS:
            if out and out[-1].isdigit():
                out.pop()                    # the fd number in `2>&1`
            skip_target = True
            continue
        out.append(tok)
    return out


def expand(token: str, env: dict) -> str:
    """`~`, `$VAR` and `${VAR}` expanded from the command's own assignments and the environment."""
    return re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", lambda m: env.get(m.group(1), m.group(0)),
                  os.path.expanduser(token))


def strip_env(tokens: list[str]) -> list[str]:
    """Drop leading VAR=value assignments."""
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        i += 1
    return tokens[i:]


def parse_git(tokens: list[str], cwd: Path) -> tuple[Path, str, list[str]]:
    """(directory git runs in, subcommand, its arguments) for a `git ...` token list."""
    repo, i = cwd, 1
    while i < len(tokens) and tokens[i].startswith("-"):
        if tokens[i] == "-C" and i + 1 < len(tokens):
            repo = (cwd / os.path.expanduser(tokens[i + 1])).resolve()
            i += 2
        elif tokens[i] in GIT_VALUE_OPTS:
            i += 2
        else:
            i += 1
    sub = tokens[i] if i < len(tokens) else ""
    return repo, sub, tokens[i + 1:]


def check_add(repo: Path, args: list[str]) -> None:
    """Every staged file must be named; wildcards and directories stage what nobody looked at."""
    for a in args:
        if a == "--":
            continue
        if a in ADD_WILDCARDS or a.endswith("/") or (not a.startswith("-") and (repo / a).is_dir()):
            block(f"`git add {a}` stages unnamed files. Name every file explicitly: git add <path> <path> ...")


def commit_problem(args: list[str]) -> str | None:
    """Why a commit form is refused: -a and pathspecs change the tree after the gates ran."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return "pathspecs after `--`"
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if name in COMMIT_TREE_CHANGING_LONG:
                return f"`{name}`"
            i += 2 if name in COMMIT_VALUE_OPTS and "=" not in a else 1
            continue
        if a.startswith("-") and len(a) > 1:
            letters, consumes = a[1:], False
            for k, ch in enumerate(letters):
                if ch in COMMIT_TREE_CHANGING_SHORT:
                    return f"`{a}`"
                if ch in COMMIT_VALUE_SHORT or ch in COMMIT_GLUED_VALUE_SHORT:   # the rest is its value (`-mfix`)
                    consumes = ch in COMMIT_VALUE_SHORT and k == len(letters) - 1
                    break
            i += 2 if consumes else 1
            continue
        return f"pathspec `{a}`"
    return None


def repo_root(path: Path) -> Path | None:
    """Top level of the worktree containing `path`, or None outside git."""
    proc = git(path, "rev-parse", "--show-toplevel")
    return Path(proc.stdout.strip()) if proc.returncode == 0 else None


def tree_dir(repo: Path, tree: str) -> Path:
    """Stamp directory for a tree; the same layout as gate.py's state_root()."""
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return tree_state(common, tree)


def stamp_ok(tdir: Path, stage: str) -> bool:
    """A stage stamp exists for this tree with a passing verdict."""
    try:
        return json.loads((tdir / f"{stage}.json").read_text()).get("verdict") in PASSING
    except (OSError, ValueError):
        return False


def fresh_token(kind: str, session: str) -> bool:
    """True when the user armed a still-valid token of `kind` in this session (not consumed)."""
    try:
        data = json.loads((TOKENS_DIR / f"{kind}.json").read_text())
    except (OSError, ValueError):
        return False
    if data.get("session") and session and data["session"] != session:
        return False
    return time.time() - float(data.get("created", 0)) <= TOKEN_TTL_S


def recorded_skip(tdir: Path) -> str | None:
    """A skip already recorded for this tree."""
    try:
        return json.loads((tdir / USER_SKIP_FILE).read_text()).get("kind")
    except (OSError, ValueError):
        return None


def consume_skip(tdir: Path, kind: str) -> None:
    """Use up the user's token and record the skip for this tree, once the skip is what let the call through."""
    (TOKENS_DIR / f"{kind}.json").unlink(missing_ok=True)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / USER_SKIP_FILE).write_text(json.dumps({"kind": kind, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}))


def missing_stamps(tdir: Path, stages: tuple[str, ...], skip: str | None) -> list[str]:
    """Stages without a passing stamp, after what `skip` waives."""
    waived = SKIP_WAIVES.get(skip or "", ())
    return [s for s in stages if s not in waived and not stamp_ok(tdir, s)]


def run_audit(repo: Path, mode: str) -> None:
    """gate.py audit; any hit (or a missing engine) blocks."""
    cmd = GATE_CMD + ["audit", "--repo", str(repo), "--mode", mode, "--guard"] \
        + (["--no-freshness"] if mode == AUDIT_BRANCH else [])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=AUDIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        block(f"could not run the {mode} audit ({exc}); fix the gate engine before committing")
    if proc.returncode != 0:
        block(f"`audit --mode {mode}` found problems:\n{proc.stdout}{proc.stderr}")


def require(repo: Path, tree: str, stages: tuple[str, ...], session: str, what: str, audit: str) -> None:
    """Block unless every stage is stamped for `tree` (or the user's skip waives it) and the audit is clean.

    A fresh skip token is consumed only after the audit passes, so a blocked call keeps it for the retry.
    """
    tdir = tree_dir(repo, tree)
    skip = recorded_skip(tdir)
    missing = missing_stamps(tdir, stages, skip)
    token = None
    if missing:            # a fresh token can waive more than the skip already recorded (#skip-gates after #skip-review)
        recorded = set(SKIP_WAIVES.get(skip or "", ()))
        token = next((k for k in (TOKEN_SKIP_GATES, TOKEN_SKIP_REVIEW)
                      if set(SKIP_WAIVES[k]) > recorded and fresh_token(k, session)), None)
        if token:
            missing = missing_stamps(tdir, stages, token)
    if missing:
        block(f"{what}: no passing {', '.join(missing)} stamp for tree {tree[:TREE_SHORT_LEN]}. "
              f"Run /gate {' and /gate '.join(missing)} first.")
    run_audit(repo, audit)
    if token:
        consume_skip(tdir, token)


def hidden_gated(tokens: list[str]) -> str | None:
    """A gated git or MR command behind a wrapper (`timeout 60 git push`, `env X=1 git commit`), or None."""
    for i in range(1, len(tokens)):
        if tokens[i] == "git" and GATED_SUBCOMMANDS & set(tokens[i + 1:]):
            return " ".join(tokens[i:i + 2])
        if tuple(tokens[i:i + 3]) in MR_COMMANDS:
            return " ".join(tokens[i:i + 3])
    return None


def guard_git(repo_dir: Path, sub: str, args: list[str], saw_index: bool, session: str) -> None:
    """Apply the git rules to one git command."""
    if sub == "add":
        check_add(repo_dir, args)
        return
    if sub not in ("commit", "push"):
        return
    repo = repo_root(repo_dir)
    if repo is None:
        block(f"cannot resolve the repository for `git {sub}` in {repo_dir}")
    if sub == "commit":
        if saw_index:
            block("change the index (`git add`, `rm`, `mv`, `restore`, `reset`, ...) and `git commit` in separate "
                  "commands, so the gate sees the final index")
        problem = commit_problem(args)
        if problem:
            block(f"`git commit` with {problem} changes the tree after the gates ran. Stage, gate, then plain `git commit`.")
        require(repo, git(repo, "write-tree").stdout.strip(), COMMIT_STAGES, session, "commit", AUDIT_STAGED)
        return
    if any(a in PUSH_FREE or a.startswith("--delete") or (a.startswith(":") and len(a) > 1) for a in args):
        return
    push_tree(repo, session, "push")


def push_tree(repo: Path, session: str, what: str) -> None:
    """A push or MR needs every stage stamped for HEAD's tree (unless the user skipped) and a clean quick audit."""
    tree = git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    require(repo, tree, PUSH_STAGES, session, what, AUDIT_BRANCH)


def guard_bash(command: str, cwd: Path, session: str) -> None:
    """Walk each simple command of a Bash call, tracking `cd`."""
    if BASH_PROTECTED.search(command):
        block("gate stamps and user tokens are written only by gate.py and the user's #tags")
    if GATE_ENGINE not in command and "git" not in command and "glab" not in command and "gh" not in command:
        return
    try:
        parts = segments(command)
    except ValueError:
        if re.search(r"\bgit\s+(commit|push|add)\b", command):
            block("could not parse this command; run the git step as its own simple command")
        return
    saw_index = saw_commit = False
    env = dict(os.environ)
    for tokens in parts:
        if not tokens:                              # a redirection on its own (`> build.log`)
            continue
        if not strip_env(tokens):                   # `VAR=value` on its own: later `cd $VAR` must resolve
            env.update(t.split("=", 1) for t in tokens)
            continue
        tokens = [expand(t, env) for t in strip_env(tokens)]
        while tokens and tokens[0] in SHELL_KEYWORDS:   # `then git commit`, `! git diff`, `{ git push; }`
            tokens = tokens[1:]
        if not tokens:
            continue
        if any(t.endswith(GATE_ENGINE) or f"/{GATE_ENGINE} " in t for t in tokens) and "stamp" in tokens \
                and "--accept" in tokens and not fresh_token(TOKEN_ACCEPT, session):
            block("`stamp --accept` needs the user's #accept-gate, typed in this session")
        if any(t.endswith(GATE_ENGINE) or f"/{GATE_ENGINE} " in t for t in tokens) and "stamp" in tokens \
                and "--approve" in tokens and not fresh_token(TOKEN_APPROVE_PLAN, session):
            block("`stamp --approve` needs the user's #approve-plan, typed in this session")
        if tokens[0] == "cd" and len(tokens) > 1:
            cwd = (cwd / tokens[1]).resolve()
            continue
        hidden = hidden_gated(tokens)
        if hidden:
            block(f"`{hidden}` runs behind `{tokens[0]}`; run it as its own simple command so the gate sees it")
        if tuple(tokens[:3]) in MR_COMMANDS:
            if saw_commit:
                block("open the MR in its own command, after the commit: the push gate must see the new HEAD")
            repo = repo_root(cwd)
            if repo is None:
                block(f"cannot resolve the repository for `{' '.join(tokens[:3])}`")
            push_tree(repo, session, "MR")
            continue
        if tokens[0] != "git":
            continue
        repo_dir, sub, args = parse_git(tokens, cwd)
        if sub == "push" and saw_commit:
            block("push in its own command, after the commit: the push gate must see the new HEAD")
        guard_git(repo_dir, sub, args, saw_index, session)
        saw_index = saw_index or sub in INDEX_CHANGING
        saw_commit = saw_commit or sub == "commit"


def crash_scan_text(raw: str) -> str:
    """What a crashed guard searches for gated git: a Bash command, nothing of a file edit, else the whole payload."""
    try:
        data = json.loads(raw)
        tool = data.get("tool_name", "")
        if tool in FILE_TOOLS:
            return ""
        if tool == "Bash":
            return str(data["tool_input"]["command"])
    except Exception:  # a payload of any other shape is searched whole: this path must never open the gate
        pass
    return raw


def guarded_main() -> None:
    """Run the guard; if it crashes, refuse a gated git command (fail closed) and let anything else through."""
    raw = sys.stdin.read()
    try:
        if IMPORT_ERROR is not None:
            raise IMPORT_ERROR
        main(raw)
    except SystemExit:
        raise
    except Exception as exc:  # any crash must not silently open the gate
        if GATED_GIT.search(crash_scan_text(raw)):
            block(f"the gate guard crashed ({exc!r}); gated git commands are refused until it is fixed")
        sys.exit(0)


def main(raw: str) -> None:
    """Parse the hook payload and allow or block."""
    try:
        data = json.loads(raw)
    except ValueError:
        if GATED_GIT.search(raw):        # an unreadable payload must not open the gate either
            block("could not read the hook payload; gated git commands are refused")
        sys.exit(0)
    tool, inp = data.get("tool_name", ""), data.get("tool_input") or {}
    if tool in FILE_TOOLS:
        if protected_path(str(inp.get("file_path") or inp.get("notebook_path") or "")):
            block("gate stamps and user tokens are written only by gate.py and the user's #tags")
        sys.exit(0)
    if tool == "Bash":
        guard_bash(str(inp.get("command", "")), Path(data.get("cwd") or os.getcwd()), str(data.get("session_id", "")))
    sys.exit(0)


if __name__ == "__main__":
    guarded_main()
