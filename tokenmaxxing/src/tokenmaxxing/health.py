"""SessionStart: silent unless something needs the user. Starts init/refresh in the background."""
import time

from gutcheck_core import config, health
from gutcheck_core.health import _bg, _stamp


def session_start(inp):
    done, notice = health.start(inp)
    if done:
        return notice
    if config.CONFIG_PATH.exists() and time.time() - _stamp("mcpwaste") > 86400:
        _bg([], module="tokenmaxxing.mcpwaste")  # MCP waste tracking + adapter agent profiles
    from . import mcpload, report
    if mcpload.pending(inp.get("cwd")):
        _bg(["mcp", "apply", inp["cwd"]])
    return report.attention_line()
