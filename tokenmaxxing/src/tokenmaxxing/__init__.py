"""tokenmaxxing: keeps text Claude doesn't need out of its context (output trims, recall, transitions, nudges)."""
from gutcheck_core import config

config.configure("tokenmaxxing", {
    "stop": {"mode": "log", "jev_fallback": "log", "max_stalls": 3, "deadline_s": 20.0,  # acts only with CLAUDE_LONG_RUN=1
             "thresholds": {"early": 0.6}},
    "adapters": [],                # MCP adapters to run; `tokenmaxxing init` fills this from configured servers
    # pass_through: MCP servers whose output is never trimmed or saved to a file (Studio calls go direct)
    "mcp_trim": {"mode": "on", "min_chars": 8000, "min_lines": 300, "pass_through": ["Roblox_Studio"]},
    "tool_trim": {"mode": "on", "min_chars": 4000},
    "read_gate": {"mode": "on", "min_chars": 30000},
    # Roblox Studio adapter. mirror: the experimental local script mirror (`tokenmaxxing studio sync` and the resync
    # after an edit), off by default. preload_tools: Roblox_Studio's tools load up front in Roblox projects instead
    # of behind ToolSearch (the server entry's alwaysLoad; adapters/roblox_studio/preload.py). Off: measured, it
    # saves one ToolSearch turn but adds all 28 tool schemas (~20k tokens) to every later call.
    "studio": {"mirror": False, "preload_tools": False},
    # Persistent soldiers (soldier.py, skill `soldiers`): past these, resuming a subagent via SendMessage gets a
    # one-line retirement suggestion. Per agent name or type: [soldier.agents.<name>]. Conservative placeholders;
    # oof's eval (soldier reuse vs fresh helper) sets the real values.
    "soldier": {"mode": "on", "max_context_tokens": 250_000, "agents": {}},  # idle limit follows the cache TTL
    "lint": {"mode": "on", "off": []},  # per-edit check (lint.py); off: project roots it stays silent in
    # Hand-back reports (handback.py): the brief asks for a `## Summary`; the parent gets the Summary + saved path.
    "handback": {"mode": "on"},
    # A subagent past this context size may only hand back (its context is mostly carry by then).
    "subagent_cap": {"mode": "on", "tokens": 250_000},
    "transitions": {"mode": "on"},  # Now-block backup, ticked-item move, /clear and restart pickup (transitions.py)
})
