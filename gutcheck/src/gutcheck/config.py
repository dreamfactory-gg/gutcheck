"""Personal config (~/.config/gutcheck/config.toml) layered over the engine's defaults.

Nothing user-specific lives in the engine; init writes the personal file.
"""
import copy
import os
import tomllib
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("GUTCHECK_CONFIG_DIR", HOME / ".config" / "gutcheck"))
CONFIG_PATH = CONFIG_DIR / "config.toml"
STATE_DIR = Path(os.environ.get("GUTCHECK_STATE_DIR", HOME / ".local" / "state" / "gutcheck"))
# Isolated runs (the token eval): plugin data goes here instead of the ${CLAUDE_PLUGIN_DATA} Claude Code sets.
DATA_DIR_OVERRIDE = os.environ.get("GUTCHECK_DATA_DIR") or None


def plugin_data():
    """gutcheck's plugin data dir as a string (override first, then Claude Code's), or None."""
    return DATA_DIR_OVERRIDE or os.environ.get("CLAUDE_PLUGIN_DATA") or None


DEFAULTS = {
    "keys": {"typesafe_service": "typesafe", "gateway_service": "ai-gateway"},
    "bash_check": {
        "mode": "log",            # "log" = logging only; "on" = emit asks
        "deadline_s": 3.0,
        "thresholds": {"stop_target": 0.5, "remote_write": 0.5, "deployish": 0.5, "script_lines": 0.5},
        "allow_prefixes": [],      # mined read-only command heads
        "approved_patterns": [],   # auto-learned "don't ask again"
        "ask_patterns": [],        # learned misses
    },
    "sensitivity": {"hosts": {}, "branches": {"main": "default branch", "master": "default branch"}, "projects": {}},
    "aliases": {},
    "stop": {"mode": "log", "jev_fallback": "log", "max_stalls": 3, "deadline_s": 20.0,  # acts only with CLAUDE_LONG_RUN=1
             "thresholds": {"early": 0.6}},
    "keyblock": {"mode": "on"},
    "adapters": [],                # MCP adapters to run; `gutcheck init` fills this from configured servers
    # pass_through: MCP servers whose output is never trimmed or saved to a file (Studio calls go direct)
    "mcp_trim": {"mode": "on", "min_chars": 8000, "min_lines": 300, "pass_through": ["Roblox_Studio"]},
    # Roblox Studio adapter. mirror: the experimental local script mirror (`gutcheck studio sync` and the resync
    # after an edit), off by default. preload_tools: Roblox_Studio's tools load up front in Roblox projects instead
    # of behind ToolSearch (the server entry's alwaysLoad; adapters/roblox_studio/preload.py). Off: measured, it
    # saves one ToolSearch turn but adds all 28 tool schemas (~20k tokens) to every later call.
    "studio": {"mirror": False, "preload_tools": False},
    # Persistent soldiers (soldier.py, skill `soldiers`): past these, resuming a subagent via SendMessage gets a
    # one-line retirement suggestion. Per agent name or type: [soldier.agents.<name>]. Conservative placeholders;
    # oof's eval (soldier reuse vs fresh helper) sets the real values.
    "soldier": {"mode": "on", "max_context_tokens": 80_000, "agents": {}},  # idle limit follows the cache TTL
    "lint": {"mode": "on", "off": []},  # per-edit check (lint.py); off: project roots it stays silent in
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


_cache = None


def load(refresh=False):
    global _cache
    if _cache is None or refresh:
        user = {}
        if CONFIG_PATH.exists():
            try:
                user = tomllib.loads(CONFIG_PATH.read_text())
            except Exception:
                user = {}
        _cache = _merge(DEFAULTS, user)
    return _cache


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(v, list):
        if not v:
            return "[]"
        return "[\n" + "".join(f"  {_toml_value(x)},\n" for x in v) + "]"
    raise TypeError(type(v))


def dumps(d, prefix=""):
    lines, tables = [], []
    for k, v in d.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            lines.append(f"{_key(k)} = {_toml_value(v)}")
    out = "\n".join(lines) + ("\n" if lines else "")
    for k, v in tables:
        name = f"{prefix}.{_key(k)}" if prefix else _key(k)
        out += f"\n[{name}]\n" + dumps(v, name)
    return out


def _key(k):
    return k if k.replace("_", "").replace("-", "").isalnum() else _toml_value(k)


def save(d):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text("# gutcheck personal config (written by `gutcheck init`; safe to edit)\n" + dumps(d))
    tmp.replace(CONFIG_PATH)
    load(refresh=True)


def user_config():
    return tomllib.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
