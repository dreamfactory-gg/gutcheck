"""SessionStart: silent unless something needs the user. Starts init/refresh in the background."""
import json
import os
import subprocess
import sys
import time

from . import config


def _bg(args, module="gutcheck.cli"):
    """Run `gutcheck <args>` (or another gutcheck module) detached; output goes to the state dir log."""
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(config.STATE_DIR / "background.log", "a")
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(__file__)))
    subprocess.Popen([sys.executable, "-m", module] + args, stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, start_new_session=True, env=env)


def _stamp(name):
    p = config.STATE_DIR / f"{name}.stamp"
    return p.stat().st_mtime if p.exists() else 0


def session_start(inp):
    from . import jev
    if not jev.have_key():
        from . import secrets
        return ("gutcheck: no TypeSafe or AI Gateway key found; Jev features are off. To enable, store one with "
                f"`{secrets.store_hint('typesafe').split('   (')[0]}` (or service `ai-gateway`), or set GUTCHECK_SECRET_TYPESAFE "
                "in the environment Claude Code starts from.")
    if not config.CONFIG_PATH.exists():
        _bg(["init", "--quiet"])
    elif time.time() - _stamp("refresh") > 86400:
        _bg(["refresh", "--quiet"])
    if config.CONFIG_PATH.exists() and time.time() - _stamp("mcpwaste") > 86400:
        _bg([], module="gutcheck.mcpwaste")  # MCP waste tracking + adapter agent profiles
    from . import report
    return report.attention_line()
