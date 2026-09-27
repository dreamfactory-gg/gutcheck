"""What the hooks did. `gutcheck report` prints a summary; attention_line() is the one-line SessionStart notice."""
import json
import time
from collections import Counter

from . import config


def _read(name, since=0):
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
    try:
        from . import stopcheck
        note = stopcheck.pop_notice()   # shown once, after the stop check promoted itself
        if note:
            return note
    except Exception:
        pass
    try:
        from . import mcpload
        note = mcpload.notice()
        if note:
            return note
    except Exception:
        pass
    week = time.time() - 7 * 86400
    s = bash_stats(week)
    if s["live_asks"] >= 10 and s["approved"] / s["live_asks"] > 0.5:
        return (f"gutcheck: {s['approved']} of the last {s['live_asks']} Bash asks were approved; "
                f"run `gutcheck report` to tune")
    if s["checked"] >= 50 and s["failed_open"] / s["checked"] > 0.2:
        return f"gutcheck: the Jev check failed open on {s['failed_open']} of {s['checked']} commands this week (outage?)"
    try:
        from . import mcpwaste
        w = mcpwaste.heavy_untightenable()
        if w:
            return (f"gutcheck: {w[0].replace('mcp__', '')} put ~{w[1] // 4000}k tokens of unused output into "
                    f"context this week and can't be trimmed further without hiding lines it uses; see `gutcheck report`")
    except Exception:
        pass
    errs = _read("hook_errors", week)
    if len(errs) >= 20:
        return f"gutcheck: {len(errs)} hook errors this week; see ~/.local/state/gutcheck/hook_errors.jsonl"
    return None


def main(args):
    days = 7
    if args and args[0].isdigit():
        days = int(args[0])
    since = time.time() - days * 86400
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
    router_report(since)
    st = _read("stops", since)
    if st:
        print(f"Stop check: {len(st)} stops; actions {dict(Counter(d.get('action') for d in st))}")
    comp = _read("compactions", since)
    if comp:
        print(f"Compactions: {len(comp)} ({sum(d.get('trigger') == 'auto' for d in comp)} automatic)")
        cps = [d for d in comp if d.get("checkpoint_tokens")]
        if cps:
            sizes = sorted(d["checkpoint_tokens"] for d in cps)
            print(f"  checkpoints: {len(cps)} written, ~{sizes[len(sizes) // 2]} tokens median, ~{sizes[-1]} max; "
                  f"{sum(1 for d in comp if d.get('checkpoint_error'))} failed")
            for d in cps[-5:]:
                print(f"    {d['t']}  {d.get('trigger')}  ~{d['checkpoint_tokens']} tokens "
                      f"(full ~{d.get('checkpoint_full_tokens')}), {d.get('rules')} rules, "
                      f"{d.get('open_items')} open items, {d.get('running')} running, {d.get('checkpoint_ms')} ms")
        inj = _read("checkpoint_inject", since)
        if inj:
            print(f"  re-injected after compaction: {sum(1 for d in inj if d.get('found'))} of {len(inj)}")
    mcp_report(since, days)
    try:
        from . import savings
        print("\n".join(savings.lines(savings.compute(days))))
    except Exception as e:  # noqa: BLE001
        print(f"Savings: unavailable ({type(e).__name__}: {e})")
    nud = _read("loop_nudge", since)
    if nud:
        print(f"Loop nudges (hand tool-heavy loops to a subagent): {len(nud)}")
    li = _read("lint", since)
    if li:
        print(f"Edit check: {len(li)} runs, {len({d['file'] for d in li if d.get('findings')})} files with findings; "
              f"by tool {dict(sum((Counter(d.get('by_tool')) for d in li), Counter()))}")
    errs = _read("hook_errors", since)
    if errs:
        print(f"Hook errors: {len(errs)}; latest: {errs[-1]['event']}: {errs[-1]['error'][:120]}")


def router_report(since):
    """Skill suggestions and doc/memory sections loaded (or, in log mode, that would have been) per prompt."""
    recs = _read("router", since)
    if not recs:
        return
    sk = [r["skill"] for r in recs if isinstance(r.get("skill"), dict)]
    sug = Counter(d["suggest"] for d in sk if d.get("suggest"))
    ctx = [r["context"] for r in recs if isinstance(r.get("context"), dict) and r["context"].get("status") == "ok"]
    loads = [c for c in ctx if c.get("sections")]
    ms = sorted(c["ms"] for c in ctx if c.get("ms") is not None)
    errs = sum(1 for d in sk if d.get("error")) + sum(1 for r in recs if isinstance(r.get("context"), dict)
                                                       and r["context"].get("error"))
    print(f"Prompt router: {len(sk)} prompts; skill suggested {sum(sug.values())} times {dict(sug.most_common(5))}; "
          f"Jev errors/timeouts {errs}")
    if ctx:
        mode = ctx[-1].get("mode") or next((r["context"].get("mode") for r in reversed(recs)
                                            if isinstance(r.get("context"), dict) and r["context"].get("mode")), "?")
        print(f"  doc/memory loading ({mode}): sections for {len(loads)} of {len(ctx)} prompts"
              + (f"; route ms p50 {ms[len(ms) // 2]}, p95 {ms[int(0.95 * (len(ms) - 1))]}" if ms else ""))


def mcp_report(since, days):
    """MCP layer: one waste line per tool, what the hooks did to MCP output, and the soldier lifecycle."""
    try:
        from . import mcpwaste
        rows = mcpwaste.summary(days)
    except Exception as e:
        rows = []
        print(f"MCP waste: unavailable ({type(e).__name__}: {e})")
    if rows:
        print(f"MCP output waste per tool (unused = never referenced afterwards; the policy tightens as it rises):")
        for _, line in rows[:25]:
            print(f"  {line}")
    trims = [d for d in _read("mcp_trim", since) if d.get("out_chars") is not None]
    if trims:
        full, out = sum(d["chars"] for d in trims), sum(d["out_chars"] for d in trims)
        live = [d for d in trims if d.get("mode") == "on"]
        print(f"MCP views: {len(trims)} outputs replaced ({len(live)} live), {full // 1000}k -> {out // 1000}k chars; "
              f"by level {dict(Counter(d.get('level') or d.get('via') for d in trims))}")
    sol = _read("soldier", since)
    if sol:
        print(f"Soldiers: {sum(1 for d in sol if d.get('note_out'))} handoff notes saved, "
              f"{sum(1 for d in sol if d.get('note_in'))} handed to a replacement, "
              f"{sum(1 for d in sol if d.get('context_tokens') is not None)} retirement suggestions")
