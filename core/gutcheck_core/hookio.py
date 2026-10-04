"""Hook plumbing both plugins share: `<plugin> hook <Event>` reads the hook JSON on stdin and prints hook JSON (or
nothing). Every hook fails open: any exception means no output, and the error is logged in the plugin's state dir.
"""
import json
import os
import sys
import threading
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


def report_since(args):
    """(days, since epoch) for `<plugin> report [DAYS]` (default 7)."""
    days = int(args[0]) if args and args[0].isdigit() else 7
    return days, time.time() - days * 86400


def errors_notice():
    """The SessionStart line when the hooks failed often this week, else None."""
    errs = read_log("hook_errors", time.time() - 7 * 86400)
    if len(errs) >= 20:
        return f"{config.APP}: {len(errs)} hook errors this week; see {config.STATE_DIR / 'hook_errors.jsonl'}"
    return None


def print_errors(since):
    errs = read_log("hook_errors", since)
    if errs:
        print(f"Hook errors: {len(errs)}; latest: {errs[-1]['event']}: {errs[-1]['error'][:120]}")


def emit(obj):
    sys.stdout.write(json.dumps(obj))


def time_left():
    """Seconds left of the prompt hook's deadline, measured from process start."""
    return max(0.0, PROMPT_DEADLINE - (time.monotonic() - _T0))


def within_deadline(fn, *args):
    """Run fn in a worker thread until the prompt deadline; True when it was still running then. A slow Jev never
    delays the prompt: whatever isn't ready is dropped and end_prompt exits at once."""
    w = threading.Thread(target=fn, args=args, daemon=True)
    w.start()
    w.join(time_left())
    return w.is_alive()


def end_prompt(inp, out, timed_out):
    """Log the prompt hook, print its output, and exit without waiting for Jev worker threads still in flight."""
    log("prompt_hook", {"session": inp.get("session_id"), "ms": int((time.monotonic() - _T0) * 1000),
                        "timed_out": timed_out})
    if out:
        emit(out)
    sys.stdout.flush()
    os._exit(0)


def _safe(name, fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        log("hook_errors", {"event": name, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]})


def main(args, handlers):
    """Run the handler for this event: "<Event>:<tool>" for tool events, "<Event>:mcp__" for any MCP tool. A tool
    event's "<Event>:*" handler runs first; when it returns hook output, that output stands and the tool's own
    handler is skipped."""
    event = args[0] if args else ""
    try:
        inp = json.load(sys.stdin)
    except Exception:
        return
    key = event
    if event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        key = f"{event}:{inp.get('tool_name')}"
        first = handlers.get(f"{event}:*")
        out = first and _safe(f"{event}:*", first, inp)
        if out:
            emit(out)
            return
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
