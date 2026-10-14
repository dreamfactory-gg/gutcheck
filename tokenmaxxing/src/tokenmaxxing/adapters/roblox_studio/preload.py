"""Preload the Studio tools in Roblox projects: Claude Code defers MCP tool schemas behind ToolSearch; a server
entry with `"alwaysLoad": true` has its tools loaded up front instead, which saves the ToolSearch round trips.

The change is one key on each Roblox project's Roblox_Studio entry in ~/.claude.json (projects.<dir>.mcpServers).
tokenmaxxing records which entries it set (state file), so `off` removes only those; an alwaysLoad the owner set is
left alone. Config: [studio] preload_tools (default off); the daily upkeep applies it.

Measured (headless, stock StudioMCP): with alwaysLoad the first turn skips ToolSearch, but every call carries all
28 Studio tool schemas (~20k tokens); deferred, ToolSearch loads only the tools used (a few hundred tokens each).
Over a long session the preload costs far more than the round trips it saves, so it stays off unless the eval
says otherwise. A Studio MCP proxy can instead mark single tools with `_meta["anthropic/alwaysLoad"]`.

  tokenmaxxing studio preload on|off|status
"""
import json
import os
import shutil
import tempfile
from pathlib import Path

from gutcheck_core import config

SERVER = "Roblox_Studio"
KEY = "alwaysLoad"


def _paths(claude_json=None, state=None):
    return (Path(claude_json or Path.home() / ".claude.json"),
            Path(state or config.STATE_DIR / "studio-preload.json"))


def _write_json(p, d):
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".gutcheck-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(d, fh, indent=2)
    shutil.copymode(p, tmp) if p.exists() else None
    os.replace(tmp, p)


def apply(on=True, claude_json=None, state=None, backup_dir=None):
    """Set (on) or remove (off, only the ones tokenmaxxing set) alwaysLoad on every project's Roblox_Studio entry.
    Returns the project dirs changed."""
    cj, st = _paths(claude_json, state)
    try:
        d = json.loads(cj.read_text())
    except (OSError, ValueError):
        return []
    try:
        ours = set(json.loads(st.read_text()).get("set", []))
    except (OSError, ValueError):
        ours = set()
    changed = []
    for proj, pv in (d.get("projects") or {}).items():
        srv = (pv.get("mcpServers") or {}).get(SERVER)
        if not isinstance(srv, dict):
            continue
        if on and not srv.get(KEY):
            srv[KEY] = True
            ours.add(proj)
            changed.append(proj)
        elif not on and proj in ours:
            if srv.get(KEY):
                srv.pop(KEY, None)
                changed.append(proj)
            ours.discard(proj)
    if changed:
        b = Path(backup_dir or config.STATE_DIR / "backups")
        b.mkdir(parents=True, exist_ok=True)
        if not (b / "claude.json.before-studio-preload").exists():
            shutil.copy2(cj, b / "claude.json.before-studio-preload")
        # re-read right before writing so a concurrent Claude Code save isn't lost; apply the same edits to it
        d2 = json.loads(cj.read_text())
        for proj in changed:
            srv = ((d2.get("projects") or {}).get(proj) or {}).get("mcpServers", {}).get(SERVER)
            if isinstance(srv, dict):
                if on:
                    srv[KEY] = True
                else:
                    srv.pop(KEY, None)
        _write_json(cj, d2)
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps({"set": sorted(ours)}))
    return changed


def status(claude_json=None, state=None):
    cj, st = _paths(claude_json, state)
    try:
        d = json.loads(cj.read_text())
    except (OSError, ValueError):
        return {"entries": 0, "preloaded": 0, "by_gutcheck": 0}
    try:
        ours = set(json.loads(st.read_text()).get("set", []))
    except (OSError, ValueError):
        ours = set()
    ents = {p: s for p, pv in (d.get("projects") or {}).items()
            if isinstance(s := (pv.get("mcpServers") or {}).get(SERVER), dict)}
    pre = [p for p, s in ents.items() if s.get(KEY)]
    return {"entries": len(ents), "preloaded": len(pre), "by_gutcheck": len([p for p in pre if p in ours])}


def upkeep():
    """Keep the config's choice applied (new Roblox projects pick it up)."""
    return apply(bool(config.load().get("studio", {}).get("preload_tools", False)))


def cli(args):
    cmd = args[0] if args else "status"
    if cmd in ("on", "off"):
        ch = apply(cmd == "on")
        print(f"Roblox_Studio alwaysLoad {cmd}: {len(ch)} project entries changed in ~/.claude.json "
              f"(restart Claude Code sessions to pick it up)")
        want = bool(config.load().get("studio", {}).get("preload_tools", False))
        if want != (cmd == "on"):
            print(f"note: set `[studio] preload_tools = {'true' if cmd == 'on' else 'false'}` in "
                  f"~/.config/tokenmaxxing/config.toml too, or the daily upkeep undoes this")
    else:
        s = status()
        print(f"Roblox_Studio entries: {s['entries']}; preloaded (alwaysLoad): {s['preloaded']} "
              f"({s['by_gutcheck']} set by tokenmaxxing)")
