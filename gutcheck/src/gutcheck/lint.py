"""Per-edit check: the linters/formatters the project itself declares, run on the edited file, plus duplicate and
comment-density checks on the edited text. Silent when clean."""
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

from . import config
from .hooks import _T0, emit, log

BUDGET = 8.0  # seconds from hook start; the hook timeout is 10
LINE = r"^.*?:(\d+):\d+: (.+)$"
PRETTIER = [".prettierrc", ".prettierrc.json", ".prettierrc.js", ".prettierrc.cjs", ".prettierrc.mjs",
            ".prettierrc.yml", ".prettierrc.yaml", ".prettierrc.toml", "prettier.config.js", ("package.json", '"prettier"')]
ESLINT = ["eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts", ".eslintrc", ".eslintrc.js",
          ".eslintrc.cjs", ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml"]
RUFF = ["ruff.toml", ".ruff.toml", ("pyproject.toml", "[tool.ruff")]
# extensions -> (tool, project signals: file names or (file, text it contains), None = always; command (the file is
# appended); finding pattern (line, message), None = a formatter whose diff gives the first changed line; skipped when
# these tools ran)
CHECKERS = {
    (".lua", ".luau"): [
        ("stylua", ["stylua.toml", ".stylua.toml"], ["stylua", "--check"], None, ()),
        ("selene", ["selene.toml"], ["selene", "--display-style=quiet"], LINE, ())],
    (".py",): [
        ("ruff", RUFF, ["ruff", "check", "--quiet", "--output-format=concise"], LINE, ()),
        ("ruff format", RUFF, ["ruff", "format", "--diff", "--quiet"], None, ()),
        ("py_compile", None, ["python3", "-m", "py_compile"], r"line (\d+)[\s\S]*?^(\w+Error: .+)", ("ruff",))],
    (".rb", ".rake"): [
        ("rubocop", [".rubocop.yml", ("Gemfile", "rubocop")], ["rubocop", "--server", "-f", "emacs"], LINE, ()),
        ("standardrb", [".standard.yml", ("Gemfile", "standard")], ["standardrb"], LINE, ()),
        ("ruby -c", None, ["ruby", "-c"], r":(\d+): (.+)", ("rubocop", "standardrb"))],
    (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"): [
        ("eslint", ESLINT, ["eslint"], r"^\s+(\d+):\d+\s+(.+)$", ()),  # stylish: ESLint 9 dropped -f unix
        ("biome", ["biome.json", "biome.jsonc"], ["biome", "check", "--reporter=github"], r"line=(\d+).*?::(.+)", ()),
        ("oxlint", [".oxlintrc.json", ("package.json", '"oxlint"')], ["oxlint", "-f", "unix"], LINE, ()),
        ("prettier", PRETTIER, ["prettier", "--check"], None, ("biome",)),
        ("oxfmt", [".oxfmtrc.json", ".oxfmtrc.jsonc"], ["oxfmt", "--check"], None, ("biome",))],
    (".go",): [("gofmt", ["go.mod"], ["gofmt", "-d", "-e"], None, ())],
    (".rs",): [("rustfmt", ["Cargo.toml"], ["rustfmt", "--check", "--color=never", "--edition", "2021"], None, ())],
}
BY_EXT = {e: rows for exts, rows in CHECKERS.items() for e in exts}
SIGNALS = sorted({s if isinstance(s, str) else s[0] for rows in CHECKERS.values() for r in rows for s in r[1] or ()})
COMMENT = {**dict.fromkeys((".lua", ".luau"), ("--",)), **dict.fromkeys((".py", ".rb", ".rake"), ("#",)),
           **dict.fromkeys((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs"), ("//", "/*", "*"))}
CACHE = config.STATE_DIR / "lint_roots.json"
BASELINE = config.STATE_DIR / "lint_pre"


def _declares(root, s):
    try:
        return (root / s).exists() if isinstance(s, str) else s[1] in (root / s[0]).read_text(errors="replace")
    except OSError:
        return False


def _command(root, sigs, cmd):
    if sigs is not None and not any(_declares(root, s) for s in sigs):
        return None
    exe = next((str(p) for d in ("node_modules/.bin", ".venv/bin") if (p := root / d / cmd[0]).exists()),
               None) or shutil.which(cmd[0])
    if exe and any(isinstance(s, tuple) and s[0] == "Gemfile" and _declares(root, s) for s in sigs or ()):
        return ["bundle", "exec", *cmd] if shutil.which("bundle") else None
    return exe and [exe, *cmd[1:]]


def available(root):
    """{tool: command} the project declares and this machine has; cached per root on the signal files' mtimes."""
    key = [os.path.getmtime(p) if (p := root / f).exists() else 0 for f in SIGNALS]
    try:
        cache = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    if (hit := cache.get(str(root))) and hit["key"] == key:
        return hit["tools"]
    tools = {r[0]: c for rows in CHECKERS.values() for r in rows if (c := _command(root, r[1], r[2]))}
    cache[str(root)] = {"key": key, "tools": tools}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache))
    return tools


def _run(cmd, path, root, pattern):
    p = subprocess.run([*cmd, path], cwd=root, capture_output=True, text=True, check=False,
                       timeout=BUDGET - (time.monotonic() - _T0))
    out = p.stdout + p.stderr
    hits = [(int(m[1]), m[2].strip()) for m in re.finditer(pattern or LINE, out, re.MULTILINE)]
    return hits or ([(_first_change(out), "not formatted")] if p.returncode and not pattern else [])


def _first_change(out):
    """stylua's numbered rows; else a unified (gofmt, ruff) or rustfmt hunk start plus its leading context lines;
    else 1 (prettier and oxfmt print no diff)."""
    if m := re.search(r"^\s*(\d+)\s+(?:\d+\s+)?\|[-+]", out, re.MULTILINE):
        return int(m[1])
    m = re.search(r"^(?:@@ -|Diff in .*:)(\d+)\D.*\n((?: .*\n|\n)*)", out, re.MULTILINE)
    return int(m[1]) + m[2].count("\n") if m else 1


def _dups(text, chunks):
    """Runs of >= 6 normalized non-trivial lines in the edited text that also appear elsewhere in the file."""
    sig = [(i, s) for i, ln in enumerate(text.splitlines(), 1) if len((s := ln.strip()).strip("(){}[];,")) > 3]
    keys = [tuple(s for _, s in w) for w in zip(*(sig[j:] for j in range(6)))]
    at = {}
    for k, key in enumerate(keys):
        at.setdefault(key, []).append(sig[k][0])
    out, seen = [], set()
    for start, chunk in chunks:
        end, k = start + chunk.count("\n"), 0
        while k < len(keys):
            if start <= sig[k][0] <= end and keys[k] not in seen and len(at[keys[k]]) > 1:
                first, other = sig[k][0], next(a for a in at[keys[k]] if a != sig[k][0])
                while k < len(keys) and len(at[keys[k]]) > 1:
                    seen.add(keys[k])
                    k += 1
                out.append((first, "dup", f"lines {first}-{sig[k + 4][0]} repeat the block at line {other}"))
            k += 1
    return out


def _comments(ext, chunks):
    lines = [s for _, c in chunks for ln in c.splitlines() if (s := ln.strip())]
    n = sum(s.startswith(COMMENT[ext]) for s in lines)
    return [(chunks[0][0], "comments", f"{n} of {len(lines)} edited lines are comments")] \
        if len(lines) >= 8 and n > 0.35 * len(lines) else []


def _target(inp):
    path = Path((inp.get("tool_input") or {}).get("file_path") or "")
    if path.suffix not in COMMENT or not path.is_file():
        return None
    root = next((d for d in path.parents if (d / ".git").exists()), Path(inp.get("cwd") or path.parent))
    cfg = config.load()["lint"]
    return None if cfg["mode"] == "off" or str(root) in cfg["off"] else (path, root)


def pre(inp):
    """Formatters already failing before the edit, so the edit isn't blamed for an unformatted file."""
    if not (t := _target(inp)):
        return
    path, root = t
    tools = available(root)
    failing = [r[0] for r in BY_EXT.get(path.suffix, []) if r[3] is None and r[0] in tools
               and _run(tools[r[0]], str(path), root, None)]
    if failing:
        BASELINE.mkdir(parents=True, exist_ok=True)
        (BASELINE / str(inp.get("tool_use_id"))).write_text(json.dumps(failing))


def hook(inp):
    if not (t := _target(inp)):
        return
    path, root = t
    ti, ext = inp.get("tool_input") or {}, path.suffix
    base = BASELINE / str(inp.get("tool_use_id"))
    try:
        was_failing = set(json.loads(base.read_text()))
        base.unlink()
    except (OSError, ValueError):
        was_failing = set()
    text = path.read_text(errors="replace")
    new = [ti["content"]] if "content" in ti else [e.get("new_string", "") for e in ti.get("edits") or [ti]]
    chunks = [(text[:i].count("\n") + 1 if (i := text.find(c)) >= 0 else 1, c) for c in new if c]
    findings = _dups(text, chunks) + _comments(ext, chunks) if chunks else []
    tools, ran, skipped = available(root), [], []
    for name, _, _, pattern, unless in BY_EXT.get(ext, []):
        if name not in tools or name in was_failing or any(u in ran for u in unless):
            continue
        try:
            findings += [(ln, name, msg) for ln, msg in _run(tools[name], str(path), root, pattern)]
            ran.append(name)
        except (subprocess.TimeoutExpired, ValueError, OSError):  # ValueError: budget already spent
            skipped.append(name)
    findings, rel = sorted(set(findings)), os.path.relpath(path, root)
    log("lint", {"session": inp.get("session_id"), "root": str(root), "file": str(path), "tools": ran,
                 "skipped": skipped, "findings": len(findings), "by_tool": Counter(f[1] for f in findings),
                 "ms": int((time.monotonic() - _T0) * 1000)})
    if findings:
        lines = [f"{rel}:{ln} {tool}: {msg[:200]}" for ln, tool, msg in findings]
        emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n".join(
            lines[:12] + ([f"(+{len(lines) - 12} more)"] if len(lines) > 12 else []))}})
