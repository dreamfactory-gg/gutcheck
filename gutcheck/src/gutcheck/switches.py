"""Off switches for the MCP output layer.

  per session:  GUTCHECK_MCP=off claude ...          (the hooks inherit the session's environment)
  per project:  [mcp] off_projects = ["~/projects/my-app"] in ~/.config/gutcheck/config.toml
                (applies when the hook's cwd is that directory or inside it)
"""
import os
from pathlib import Path

from . import config


def mcp_layer_off(inp=None):
    if os.environ.get("GUTCHECK_MCP", "").strip().lower() in ("off", "0", "false", "no"):
        return True
    cwd = (inp or {}).get("cwd") or os.getcwd()
    try:
        here = Path(cwd).expanduser().resolve()
    except OSError:
        return False
    for p in config.load().get("mcp", {}).get("off_projects", []):
        try:
            root = Path(p).expanduser().resolve()
        except OSError:
            continue
        if here == root or root in here.parents:
            return True
    return False
