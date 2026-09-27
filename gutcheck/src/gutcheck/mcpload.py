"""Idle MCP load: what each user-scope MCP server adds to every request just by being connected, how many recent
sessions called it, and a default-off list applied per project (Claude Code only disables servers per project).

  gutcheck mcp [--measure]     table + suggestions; --measure re-runs the probes (one `claude -p` turn per server
                               plus a baseline, --strict-mcp-config, about $0.10 each on the default model)
  gutcheck mcp off|on NAME...  edit `[mcp_servers] default_off` and every project in ~/.claude.json
SessionStart applies default_off once to each project it hasn't seen, so a later /mcp re-enable there sticks.
"""
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, transcripts

CLAUDE_JSON = Path.home() / ".claude.json"
LOAD = config.STATE_DIR / "mcp_load.json"
APPLIED = config.STATE_DIR / "mcp_applied.json"   # {project: [servers gutcheck disabled there]}
MEASURE_DIR = config.STATE_DIR / "mcp-measure"
USE_DAYS = 30
RARE = 0.05   # suggest off below 1 session in 20
CALL_RE = r'"type":"tool_use","id":"[^"]+","name":"mcp__[A-Za-z0-9_-]+?__'


def _read(p, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _write(p, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".gutcheck.tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(p)


def user_servers():
    return _read(CLAUDE_JSON, {}).get("mcpServers") or {}


def default_off():
    return config.user_config().get("mcp_servers", {}).get("default_off", [])


def _slug(name):
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def _turn(servers):
    """Input tokens of a one-turn session with only these MCP servers, and the servers' statuses."""
    MEASURE_DIR.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(["claude", "-p", "reply ok", "--output-format", "stream-json", "--verbose", "--max-turns", "1",
                          "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": servers})],
                         cwd=MEASURE_DIR, capture_output=True, text=True, timeout=300, check=False,
                         env=dict(os.environ, GUTCHECK_NESTED="1")).stdout
    tokens, status, tools = None, {}, 0
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("subtype") == "init":
            status = {m["name"]: m["status"] for m in d.get("mcp_servers", [])}
            tools = sum(t.startswith("mcp__") for t in d.get("tools", []))
        elif d.get("type") == "result":
            u = d["usage"]
            tokens = u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"]
    return tokens, status, tools


def measure():
    servers = user_servers()
    jobs = {None: {}} | {n: {n: c} for n, c in servers.items()}
    def probe(cfg):
        try:
            return _turn(cfg)
        except subprocess.TimeoutExpired:
            return None, {}, 0
    with ThreadPoolExecutor(len(jobs)) as ex:
        res = dict(zip(jobs, ex.map(probe, jobs.values())))
    base = res.pop(None)[0]
    load = {n: {"tokens": t - base if t and base else None, "status": s.get(n), "tools": k}
            for n, (t, s, k) in res.items()}
    _forget_measure_project()
    meta = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "baseline": base, "servers": load}
    _write(LOAD, meta)
    meta["notice"] = _notice_text(table())   # computed here (background), not in the 10 s SessionStart hook
    _write(LOAD, meta)
    return load


def _forget_measure_project():
    d = _read(CLAUDE_JSON, None)
    if d and d.get("projects", {}).pop(str(MEASURE_DIR.resolve()), None) is not None:
        _write(CLAUDE_JSON, d)


def usage(days=USE_DAYS):
    """(sessions that called each server's tools, main sessions) in the window; subagent calls count for their session."""
    files = [str(f) for f in transcripts.files(time.time() - days * 86400) if MEASURE_DIR.name not in str(f)]
    main = sum(Path(f).parent.parent == transcripts.PROJECTS for f in files)
    hits = subprocess.run(["grep", "-oHE", CALL_RE, *files], capture_output=True, text=True, check=False).stdout if files else ""
    seen = set()
    for line in hits.splitlines():
        path, call = line.split(':"type"', 1)
        session = Path(path).relative_to(transcripts.PROJECTS).parts[1].removesuffix(".jsonl")
        seen.add((call.rsplit('"name":"mcp__', 1)[1][:-2], session))
    used = Counter(slug for slug, _ in seen)
    return {n: used[_slug(n)] for n in user_servers()}, main


def table(remeasure=False):
    load = measure() if remeasure or not LOAD.exists() else _read(LOAD, {}).get("servers", {})
    used, n = usage()
    off = default_off()
    rows = []
    for name in user_servers():
        m = load.get(name, {})
        share = used[name] / n if n else 0
        suggest = "off" if (m.get("tokens") or 0) > 0 and share < RARE else "keep"
        rows.append((name, m.get("status"), m.get("tools"), m.get("tokens"), used[name], n,
                     "off" if name in off else "on", suggest))
    return rows


def notice():
    """The measurement's SessionStart line, shown once."""
    meta = _read(LOAD, {})
    text = meta.pop("notice", None)
    if text:
        _write(LOAD, meta)
    return text


def _notice_text(rows):
    rows = [r for r in rows if r[7] == "off" and r[6] == "on"]
    if rows:
        return ("gutcheck: MCP servers " + ", ".join(f"{r[0]} (+{r[3]} tokens every request, used in {r[4]} of "
                f"{r[5]} sessions)" for r in rows) + ". Show the user `gutcheck mcp` and ask which to turn off "
                "(`gutcheck mcp off NAME...`).")


def _edit(projects, names, add, tries=3):
    """Add or remove names in each project's disabledMcpServers. A running Claude Code session may rewrite
    ~/.claude.json from its own copy, so check after a pause and redo what it dropped."""
    for _ in range(tries):
        d = _read(CLAUDE_JSON, None)
        if d is None:
            return False
        for p in projects:
            pv = d.setdefault("projects", {}).setdefault(p, {})
            cur = pv.get("disabledMcpServers", [])
            pv["disabledMcpServers"] = sorted(set(cur) | set(names)) if add else [s for s in cur if s not in names]
        _write(CLAUDE_JSON, d)
        time.sleep(3)
        got = _read(CLAUDE_JSON, {}).get("projects", {})
        now = [set(got.get(p, {}).get("disabledMcpServers", [])) for p in projects]
        if all(set(names) <= s if add else not set(names) & s for s in now):
            return True
    return False


def _mark(projects, names, add):
    applied = _read(APPLIED, {})
    for p in projects:
        cur = set(applied.get(p, []))
        applied[p] = sorted(cur | set(names) if add else cur - set(names))
    _write(APPLIED, applied)


def set_default(names, off):
    unknown = [n for n in names if n not in user_servers()]
    if unknown:
        sys.exit(f"gutcheck mcp: not a user-scope server: {', '.join(unknown)} (have: {', '.join(user_servers())})")
    user = config.user_config()
    cur = set(user.setdefault("mcp_servers", {}).get("default_off", []))
    user["mcp_servers"]["default_off"] = sorted(cur | set(names) if off else cur - set(names))
    config.save(user)
    if off:   # mark only where gutcheck adds it, so `on` never undoes the user's own per-project disables
        projects = [p for p, v in _read(CLAUDE_JSON, {}).get("projects", {}).items()
                    if set(names) - set(v.get("disabledMcpServers", []))]
    else:
        projects = [p for p, s in _read(APPLIED, {}).items() if set(names) & set(s)]
    ok = _edit(projects, names, add=off)
    _mark(projects, names, add=off)
    print(f"{'off' if off else 'on'} by default: {', '.join(names)} ({len(projects)} projects"
          f"{'' if ok else '; a running session kept overwriting ~/.claude.json, rerun after it exits'})")


def project_key(cwd):
    """Claude Code keys ~/.claude.json projects by git root, else the real path."""
    top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                         check=False).stdout.strip()
    return top or os.path.realpath(cwd)


def pending(cwd):
    """default_off servers not yet applied to this project (SessionStart; no writes)."""
    if not cwd or Path(cwd).resolve().is_relative_to(MEASURE_DIR.resolve()):
        return []
    key = project_key(cwd)
    return [s for s in default_off() if s not in _read(APPLIED, {}).get(key, [])]


def apply(cwd):
    """Background, from SessionStart: wait for Claude Code to write the project's entry, then disable."""
    names = pending(cwd)
    if names:
        time.sleep(5)
        key = project_key(cwd)
        if _edit([key], names, add=True):
            _mark([key], names, add=True)


def main(args):
    if args[:1] == ["apply"]:
        return apply(args[1])
    if args[:1] in (["off"], ["on"]):
        if len(args) < 2:
            sys.exit(__doc__)
        return set_default(args[1:], args[0] == "off")
    rows = table("--measure" in args)
    meta = _read(LOAD, {})
    print(f"MCP servers (user scope). Baseline request without MCPs: {meta.get('baseline')} tokens "
          f"(measured {meta.get('at')}); tokens below ride on every request.")
    print(f"{'server':<22}{'status':<12}{'tools':>6}{'+tokens':>9}  {'sessions used':<19}{'now':<5}suggest")
    for name, status, tools, tokens, used, n, now, sug in rows:
        print(f"{name:<22}{status or '?':<12}{tools if tools is not None else '?':>6}"
              f"{tokens if tokens is not None else '?':>9}  {f'{used} of {n} ({USE_DAYS}d)':<19}{now:<5}{sug}")
