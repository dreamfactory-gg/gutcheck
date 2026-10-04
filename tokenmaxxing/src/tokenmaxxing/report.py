"""What tokenmaxxing's hooks did. `tokenmaxxing report` prints a summary; attention_line() is the one-line SessionStart notice."""
from collections import Counter

from gutcheck_core import hookio
from gutcheck_core.hookio import read_log as _read


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
    try:
        from . import mcpwaste
        w = mcpwaste.heavy_untightenable()
        if w:
            return (f"tokenmaxxing: {w[0].replace('mcp__', '')} put ~{w[1] // 4000}k tokens of unused output into "
                    f"context this week and can't be trimmed further without hiding lines it uses; see `tokenmaxxing report`")
    except Exception:
        pass
    return hookio.errors_notice()


def main(args):
    days, since = hookio.report_since(args)
    print(f"tokenmaxxing report, last {days} days")
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
    hb = [d for d in _read("handback", since) if d.get("via")]
    if hb:
        cut = [d for d in hb if d["decision"] == "summary"]
        print(f"Hand-back reports: {len(hb)}; {len(cut)} cut to Summary + path "
              f"({sum(d['chars'] for d in cut) // 1000}k -> {sum(d['out_chars'] for d in cut) // 1000}k chars), "
              f"{sum(d['decision'] == 'denied' for d in hb)} refused once for no Summary")
    cap = _read("subagent_cap", since)
    if cap:
        print(f"Subagent context cap: {len(cap)} tool calls refused in {len({d.get('agent') for d in cap})} agents")
    li = _read("lint", since)
    if li:
        print(f"Edit check: {len(li)} runs, {len({d['file'] for d in li if d.get('findings')})} files with findings; "
              f"by tool {dict(sum((Counter(d.get('by_tool')) for d in li), Counter()))}")
    hookio.print_errors(since)


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
        print("MCP output waste per tool (unused = never referenced afterwards; the policy tightens as it rises):")
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
