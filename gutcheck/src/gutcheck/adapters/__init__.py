"""MCP adapters: server-specific behaviour on top of the generic MCP output core (mcpout.py).

Each adapter is a package here exposing SERVER (the MCP server name in tool names, mcp__<SERVER>__<tool>),
post_tool(tool, inp, text, cfg) -> compact view or None, and optionally cli(args) and rules(). An adapter runs
only when listed in the personal config's `adapters`, which `gutcheck init` fills from the MCP servers actually
configured on this machine. Nothing is imported otherwise.
"""
import importlib
import json
from pathlib import Path

from .. import config

KNOWN = {"Roblox_Studio": "roblox_studio"}


def enabled():
    return [a for a in config.load().get("adapters", []) if a in KNOWN.values()]


def for_server(server):
    mod = KNOWN.get(server)
    if mod and mod in enabled():
        return importlib.import_module(f"{__name__}.{mod}")
    return None


def configured_servers():
    """MCP server names configured for Claude Code on this machine (user and per-project, plus .mcp.json)."""
    names = set()
    try:
        d = json.loads((Path.home() / ".claude.json").read_text())
        names |= set((d.get("mcpServers") or {}).keys())
        for pv in (d.get("projects") or {}).values():
            names |= set((pv.get("mcpServers") or {}).keys())
    except (OSError, ValueError):
        pass
    for p in (Path.home() / "projects").glob("*/.mcp.json"):
        try:
            names |= set((json.loads(p.read_text()).get("mcpServers") or {}).keys())
        except (OSError, ValueError):
            pass
    return names


def detect():
    return sorted(KNOWN[s] for s in configured_servers() if s in KNOWN)


def projects_using(server):
    """Project directories whose Claude Code config enables this MCP server."""
    out = []
    try:
        d = json.loads((Path.home() / ".claude.json").read_text())
        for proj, pv in (d.get("projects") or {}).items():
            if server in (pv.get("mcpServers") or {}):
                out.append(proj)
    except (OSError, ValueError):
        pass
    return out
