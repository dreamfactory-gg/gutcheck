"""Hook plumbing both plugins share: `<plugin> hook <Event>` reads the hook JSON on stdin and prints hook JSON (or
nothing). Every hook fails open: any exception means no output, and the error is logged in the plugin's state dir.
"""
import json
import os
import sys
import time
import traceback

from . import config

_T0 = time.monotonic()
PROMPT_DEADLINE = float(os.environ.get("GUTCHECK_PROMPT_DEADLINE", "1.4"))  # seconds from hook start


def log(name, rec):
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}
        with open(config.STATE_DIR / f"{name}.jsonl", "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def read_log(name, since=0):
    """Records of one hook log (`log(name, ...)`), optionally only those since an epoch time."""
    p = config.STATE_DIR / f"{name}.jsonl"
    out = []
    if not p.exists():
        return out
    for line in p.open():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if since and time.mktime(time.strptime(d["t"], "%Y-%m-%dT%H:%M:%S")) < since:
            continue
        out.append(d)
    return out


def emit(obj):
    sys.stdout.write(json.dumps(obj))


def _safe(name, fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        log("hook_errors", {"event": name, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]})


def main(args, handlers):
    """Run the handler for this event: "<Event>:<tool>" for tool events, "<Event>:mcp__" for any MCP tool."""
    event = args[0] if args else ""
    try:
        inp = json.load(sys.stdin)
    except Exception:
        return
    key = event
    if event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        key = f"{event}:{inp.get('tool_name')}"
    h = handlers.get(key)
    if h is None and str(inp.get("tool_name", "")).startswith("mcp__"):
        h = handlers.get(f"{event}:mcp__")
    if not h:
        return
    try:
        h(inp)
    except Exception as e:
        log("hook_errors", {"event": key, "error": f"{type(e).__name__}: {e}",
                            "trace": traceback.format_exc()[-1500:]})
