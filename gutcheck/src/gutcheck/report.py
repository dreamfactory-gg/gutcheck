"""What gutcheck's hooks did. `gutcheck report` prints a summary; attention_line() is the one-line SessionStart notice."""
import time
from collections import Counter

from gutcheck_core import hookio
from gutcheck_core.hookio import read_log as _read


def bash_stats(since=0):
    checks = _read("bashcheck", since)
    ran = {d["tool_use_id"] for d in _read("bash_ran", since) if d.get("tool_use_id")}
    asks = [d for d in checks if d.get("decision") == "ask"]
    live_asks = [d for d in asks if d.get("mode") == "on"]
    approved = [d for d in live_asks if d.get("tool_use_id") in ran]
    failed_open = [d for d in checks if d.get("stage") == "jev-failed-open"]
    return {"checked": len(checks), "asks": len(asks), "live_asks": len(live_asks), "approved": len(approved),
            "failed_open": len(failed_open), "stages": Counter(d.get("stage") for d in checks),
            "reasons": Counter((d.get("reason") or "")[:70] for d in asks)}


def attention_line():
    """One line when something needs a human decision, else None."""
    week = time.time() - 7 * 86400
    s = bash_stats(week)
    if s["live_asks"] >= 10 and s["approved"] / s["live_asks"] > 0.5:
        return (f"gutcheck: {s['approved']} of the last {s['live_asks']} Bash asks were approved; "
                f"run `gutcheck report` to tune")
    if s["checked"] >= 50 and s["failed_open"] / s["checked"] > 0.2:
        return f"gutcheck: the Jev check failed open on {s['failed_open']} of {s['checked']} commands this week (outage?)"
    return hookio.errors_notice()


def main(args):
    days, since = hookio.report_since(args)
    s = bash_stats(since)
    print(f"gutcheck report, last {days} days")
    print(f"Bash check: {s['checked']} commands checked; stages {dict(s['stages'])}")
    print(f"  asks: {s['asks']} ({s['live_asks']} shown to you, {s['approved']} of those approved)")
    for r, n in s["reasons"].most_common(15):
        print(f"    {n:4d}  {r}")
    kb = _read("keyblock", since)
    blocked = [d for d in kb if d.get("kinds")]
    warned = sum(1 for d in kb if d.get("action") == "warn")
    print(f"Key blocker: {len(blocked)} prompts blocked"
          + (f" ({dict(Counter(k for d in blocked for k in d['kinds']))})" if blocked else "")
          + (f", {warned} warnings" if warned else ""))
    hookio.print_errors(since)
