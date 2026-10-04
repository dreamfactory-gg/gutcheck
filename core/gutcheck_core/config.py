"""Personal config (~/.config/<plugin>/config.toml) layered over the engine's defaults.

Each plugin (gutcheck, tokenmaxxing) calls configure() from its package __init__ with its name and defaults, so
one process serves one plugin: its own config file, state dir and plugin data. Nothing user-specific lives in the
engine; init writes the personal file.
"""
import copy
import os
import tomllib
from pathlib import Path

HOME = Path.home()
# The one venv both plugins share; also holds what both plugins read and write (the standing-rules store).
SHARED_HOME = Path(os.environ.get("GUTCHECK_HOME", HOME / ".local" / "share" / "gutcheck"))
RULES_DIR = Path(os.environ.get("GUTCHECK_RULES_DIR", SHARED_HOME / "rules"))
CORE_DEFAULTS = {"keys": {"typesafe_service": "typesafe", "gateway_service": "ai-gateway"}}


def configure(app, defaults):
    """Set this process's plugin: its config and state dirs (overridable by <APP>_CONFIG_DIR and <APP>_STATE_DIR)
    and, for isolated runs such as the token eval, <APP>_DATA_DIR in place of the ${CLAUDE_PLUGIN_DATA} Claude Code
    sets."""
    global APP, CONFIG_DIR, CONFIG_PATH, STATE_DIR, DATA_DIR_OVERRIDE, DEFAULTS
    env = app.upper()
    APP = app
    CONFIG_DIR = Path(os.environ.get(f"{env}_CONFIG_DIR", HOME / ".config" / app))
    CONFIG_PATH = CONFIG_DIR / "config.toml"
    STATE_DIR = Path(os.environ.get(f"{env}_STATE_DIR", HOME / ".local" / "state" / app))
    DATA_DIR_OVERRIDE = os.environ.get(f"{env}_DATA_DIR") or None
    DEFAULTS = {**CORE_DEFAULTS, **defaults}


def plugin_data():
    """The plugin's data dir as a string (override first, then Claude Code's), or None."""
    return DATA_DIR_OVERRIDE or os.environ.get("CLAUDE_PLUGIN_DATA") or None


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
    tmp.write_text(f"# {APP} personal config (written by `{APP} init`; safe to edit)\n" + dumps(d))
    tmp.replace(CONFIG_PATH)
    load(refresh=True)


def user_config():
    return tomllib.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
