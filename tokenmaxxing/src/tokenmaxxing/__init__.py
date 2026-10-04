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
    # Hand-back reports (handback.py): the parent gets the part before `## Details` plus the saved path.
    "handback": {"mode": "on"},
    # A subagent past this context size may only hand back (its context is mostly carry by then). log: measured, 15%
    # of subagents cross 250k mid-task with a median 49 calls left, and warm agents resumed over it would hand back
    # with no work done.
    "subagent_cap": {"mode": "log", "tokens": 250_000},
    # Compact the main session between turns once its context passes main_tokens (hooks/register.ts), at most once
    # every 3 turns. Absolute, not a share of the window: what a token costs doesn't depend on the window's size.
    "compact": {"mode": "log", "main_tokens": 400_000},
    # Fixed prefix (hooks/register.ts), decided once per session: no total_tokens_reminder, no skill listing in
    # subagents, no MCP instructions of servers no session called in 30 days (as the last refresh counted).
    "prefix": {"mode": "on"},
    # Long tool inputs (hooks/register.ts): a Bash command or Write content over these sizes is kept in the transcript
    # as a stub naming its saved copy; the call itself runs with the original.
    "long_input": {"mode": "log", "bash_chars": 2000, "write_chars": 4000},
    "transitions": {"mode": "on"},  # Now-block backup, ticked-item move, /clear and restart pickup (transitions.py)
})
