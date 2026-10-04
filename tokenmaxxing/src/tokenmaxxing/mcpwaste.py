"""Continuous waste tracking for every MCP tool (runs at refresh, in the background; no Claude tokens).

For each MCP call in the transcripts: how much of its output entered context, how much of that the agent went on
to use (the used-lines proxy: lines holding identifiers the agent reused in its next messages or calls), and
whether a tokenmaxxing view's saved file was read back afterwards. Every call is also replayed at each level of the
ladder (pass < headtail < outline < receipt) to see what each view would have cost and whether it would have hidden
a line the agent used.

Policy moves one step per refresh (mcp_policy.json, history kept in each entry):
  - tighten when the tool's waste is high (most of what it puts in context goes unused) and the tighter view
    would not have hidden used lines; it's lossless, the full output is always saved;
  - loosen only when the tool's saved files keep being re-read (live), or, before any live data, when replay
    shows the current view would have hidden lines the agent used.
`tokenmaxxing report` prints one waste line per tool; SessionStart says one line only when a tool wastes heavily and
can't be tightened further.
"""
import json
import re
import time
from datetime import datetime

from gutcheck_core import config

from . import mcpout

CALLS = "mcp_calls.jsonl"
WINDOW_DAYS = 30
WINDOW_CALLS = 200
MIN_N = 5
LOOSEN_AT = 0.35       # re-read (or would-hide) rate that loosens a step
TIGHTEN_MAX_MISS = 0.2  # the tighter view may hide used lines in at most this share of calls
TIGHTEN_WASTE = 0.5     # share of the current view that goes unused
TIGHTEN_MIN_CHARS = 1000  # mean unused chars per call
HEAVY_WEEK_CHARS = 150_000
REREAD_CHARS = 3000   # a follow-up read: the call, its framing and the reply around it
_VIEW_TAG = re.compile(r"saved to (\S*/mcp-out/\S+?\.(?:txt|json|log|luau|lua|py|ts|rs|go))\b")


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{5,}")
_CODEY = re.compile(r"[_0-9]|[a-z][A-Z]|^[A-Z]{4,}$")


def _idents(text):
    """Code-like identifiers only (snake_case, camelCase, digits, ALLCAPS): plain words like 'missing' or
    'Server' appear in any prose and would make every log line look used."""
    return {w for w in _IDENT.findall(text) if _CODEY.search(w)}


def used_lines(out, after, context):
    """Lines of `out` holding identifiers the agent reused afterwards and hadn't already written before the call."""
    ids = (_idents(after) - _idents(context)) & _idents(out)
    return [l for l in out.splitlines() if l.strip() and any(x in l for x in ids)]


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def _after_text(evs, j, call_id):
    parts = []
    for e in evs[j + 1:j + 14]:
        if e["kind"] == "text":
            parts.append(e["text"])
        elif e["kind"] == "tool" and e.get("id") != call_id:
            parts.append(json.dumps(e.get("input", ""))[:3000])
    return "\n".join(parts)


def _reread(evs, j, path):
    """Was the saved file read back (Read/Grep/Bash naming it) in the next few steps?"""
    base = path.rsplit("/", 1)[-1]
    for e in evs[j + 1:j + 20]:
        if e["kind"] == "tool" and base in json.dumps(e.get("input", "")):
            return True
        if e["kind"] == "prompt" and not e.get("meta"):
            break
    return False


def _full_text(entered):
    """The full output behind a tokenmaxxing view (from its saved file), else the text as it entered context."""
    m = _VIEW_TAG.search(entered[:600]) or _VIEW_TAG.search(entered)
    if m:
        try:
            with open(m.group(1)) as fh:
                return fh.read(), m.group(1)
        except OSError:
            return None, m.group(1)
    return entered, None


def _simulate(tool, full, kind, used, routine, cfg):
    sim = {}
    for lv in mcpout.LEVELS:
        v = mcpout.render(tool, full, kind, lv, cfg, None, routine, None) if lv != "pass" else None
        v = full if v is None else v
        uniq = list(dict.fromkeys(l.strip() for l in used))
        present = [l for l in uniq if l in v]
        missing = [l for l in uniq if l not in v]
        # [view chars, hid a used line?, used chars shown, used chars hidden]
        sim[lv] = [len(v), int(bool(missing)), min(len(v), sum(len(l) for l in present)), sum(len(l) for l in missing)]
    return sim


def scan(since=0.0, known=None):
    """New per-call records from transcripts modified after `since` (deduplicated by tool_use id)."""
    from gutcheck_core import transcripts

    from . import adapters
    known = known if known is not None else set()
    cfg = config.load().get("mcp_trim", {})
    raw = []
    for f in transcripts.files(since=since):
        evs = list(transcripts.events(f))
        pos = {}
        for i, ev in enumerate(evs):
            if ev["kind"] == "result" and ev["id"] in pos:
                i0 = pos.pop(ev["id"])
                c = evs[i0]
                raw.append((c, ev, evs, i0, i))
            elif ev["kind"] == "tool" and (ev["name"] or "").startswith("mcp__") and ev["id"] not in known:
                pos[ev["id"]] = i
                known.add(ev["id"])
    raw.sort(key=lambda r: r[0].get("ts") or "")
    live_levels = {}
    try:
        for line in (config.STATE_DIR / "mcp_trim.jsonl").open():
            d = json.loads(line)
            if d.get("tool_use_id") and d.get("mode") == "on":
                live_levels[d["tool_use_id"]] = d.get("level") or "headtail"
    except (OSError, ValueError):
        pass
    routine_counts, recs = {}, []
    for c, res, evs, i, j in raw:
        name = c["name"]
        entered = res["text"] or ""
        if not entered:
            continue
        parts = name.split("__")
        ad = adapters.for_server(parts[1]) if len(parts) >= 3 else None
        full, saved = _full_text(entered)
        kind, per_call = mcpout.kind_for(name, c["input"], full or entered, ad)
        key = mcpout.policy_key(name, kind, per_call)
        live = saved is not None
        context = "\n".join(e["text"] for e in evs[max(0, i - 6):i] if e["kind"] == "text") + json.dumps(c["input"])
        after = _after_text(evs, j, c["id"])
        used = used_lines(full or entered, after, context)
        counts = routine_counts.setdefault(name, {}) if kind == "log" else None
        routine = {k for k, n in counts.items() if n >= 2} if counts is not None else None
        rec = {"id": c["id"], "t": _ts(c.get("ts")), "tool": name, "key": key, "kind": kind,
               "entered": len(entered), "full": len(full) if full else None, "error": res["error"],
               "used": sum(len(l) for l in used_lines(entered, after, context))}
        if live:
            rec["live_level"] = live_levels.get(c["id"], "headtail")  # views before levels existed
            rec["reread"] = _reread(evs, j, saved)
        if full and not res["error"]:
            rec["sim"] = _simulate(name, full, kind, used, routine, cfg)
        if counts is not None and full:
            for k in {mcpout.line_key(l) for l in full.splitlines() if l.strip()}:
                counts[k] = counts.get(k, 0) + 1
        recs.append(rec)
    return recs


def load_calls():
    p = config.STATE_DIR / CALLS
    out = []
    if p.exists():
        for line in p.open():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def stats(recs, level):
    """Waste, re-read and net-cost numbers for these calls at `level` (replay; live re-reads when there are any).
    net = chars in context + for each call whose view hid a line the agent used, one follow-up read
    (REREAD_CHARS for the round trip plus the hidden lines)."""
    sims = [r for r in recs if r.get("sim")]
    if not sims:
        return {"n": 0}
    def actual(r):  # trimmed live at this level: what really entered context (adapter views included)
        return r.get("live_level") == level and "reread" in r
    vlen = sum(r["entered"] if actual(r) else r["sim"][level][0] for r in sims)
    used = sum(min(r["used"], r["entered"]) if actual(r) else r["sim"][level][2] for r in sims)
    hid = [r for r in sims if r["sim"][level][1]]
    net = vlen + sum(REREAD_CHARS + r["sim"][level][3] for r in hid)
    live = [r for r in recs if r.get("live_level") == level and "reread" in r]
    reread = None
    if len(live) >= MIN_N:
        reread = sum(r["reread"] for r in live) / len(live)
        net = vlen + reread * len(sims) * REREAD_CHARS + sum(r["sim"][level][3] for r in hid)
    return {"n": len(sims), "chars": vlen, "waste": max(0, vlen - used),
            "waste_ratio": (vlen - used) / vlen if vlen else 0.0, "mean_waste": (vlen - used) / len(sims),
            "would_hide": len(hid) / len(sims), "live_n": len(live), "reread": reread, "net": net}


def decide(recs, level, kind):
    """(new level, reason) for one step, or (level, None)."""
    L = mcpout.LEVELS
    top = L.index(mcpout.MAX_LEVEL.get(kind, "outline"))
    cur = L.index(level) if level in L else 0
    s = stats(recs, level)
    if s["n"] < MIN_N:
        return level, None
    reread = s["reread"] if s["reread"] is not None else s["would_hide"]
    if cur > 0 and reread >= LOOSEN_AT:
        prev = stats(recs, L[cur - 1])
        if prev["net"] < s["net"]:
            src = "its saved files were re-read" if s["reread"] is not None else \
                "replay: the view hid lines the agent used"
            return L[cur - 1], (f"loosen: {src} in {reread:.0%} of {s['n']} calls; {L[cur - 1]} costs "
                                f"{prev['net'] / s['net']:.0%} as much")
    if cur < top and s["waste_ratio"] >= TIGHTEN_WASTE and s["mean_waste"] >= TIGHTEN_MIN_CHARS:
        nxt = L[cur + 1]
        t = stats(recs, nxt)
        if t["would_hide"] < LOOSEN_AT and t["net"] < 0.9 * s["net"]:
            return nxt, (f"tighten: {s['waste_ratio']:.0%} of its output unused (~{int(s['mean_waste'])} chars/call); "
                         f"{nxt} costs {t['net'] / s['net']:.0%} as much, hides used lines in {t['would_hide']:.0%}")
    return level, None


def _window(recs, now):
    recent = [r for r in recs if r["t"] >= now - WINDOW_DAYS * 86400]
    return sorted(recent, key=lambda r: r["t"])[-WINDOW_CALLS:]


def learned_kind(tool, recs):
    """Kind from the tool's name, corrected by what its outputs look like: mostly log-shaped lines -> log."""
    k = mcpout.name_kind(tool)
    fulls = [r for r in recs if r.get("full")]
    if k == "read" and len(fulls) >= MIN_N:
        big = [r for r in fulls if (r.get("full") or 0) > 2000]
        if big and len(big) >= len(fulls) / 2 and re.search(r"(^|_)(get|read)_", tool.split("__")[-1]) and \
                re.search(r"(console|log|output)", tool.split("__")[-1]):
            k = "log"
    return k


def track(full=False, now=None):
    """Update per-call records from new transcripts and move each tool's level (bootstrap: until stable)."""
    now = now or time.time()
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = config.STATE_DIR / "mcpwaste.stamp"
    old = [] if full else load_calls()
    since = 0.0 if full or not stamp.exists() else stamp.stat().st_mtime - 3600
    known = {r["id"] for r in old}
    new = scan(since, known)
    calls = [r for r in old + new if r["t"] >= now - 90 * 86400]
    p = config.STATE_DIR / CALLS
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in calls))
    tmp.replace(p)
    stamp.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
    pol = dict(mcpout.policy())
    if full:  # rebuild, but keep entries only older hook versions read (no level)
        pol = {k: v for k, v in pol.items() if "level" not in v}
    by_key = {}
    for r in calls:
        by_key.setdefault(r["key"], []).append(r)
    for key, recs in by_key.items():
        win = _window(recs, now)
        tool = key.split("#")[0]
        kind = key.split("#")[1] if "#" in key else recs[-1]["kind"]
        e = pol.get(key) or {}
        if "#" not in key and not e.get("kind"):
            e["kind"] = kind
        level = e.get("level") or mcpout.DEFAULT_LEVEL.get(kind, "headtail")
        hist = e.get("history", [])
        for _ in range(len(mcpout.LEVELS) if full or not e.get("level") else 1):
            nxt, why = decide(win, level, kind)
            if not why:
                break
            hist.append({"t": time.strftime("%Y-%m-%d"), "from": level, "to": nxt, "why": why})
            level = nxt
        s = stats(win, level)
        e.update(level=level, n=s["n"], waste_ratio=round(s.get("waste_ratio", 0), 3),
                 waste_chars=s.get("waste", 0), would_hide=s.get("would_hide"), reread=s.get("reread"),
                 history=hist[-10:], tool=tool)
        e["mode"] = "pass" if level == "pass" else "trim"  # read by older hook versions
        e.pop("ratio", None)
        pol[key] = e
    ptmp = config.STATE_DIR / (mcpout.POLICY + ".tmp")
    ptmp.write_text(json.dumps(pol, indent=1, sort_keys=True))
    ptmp.replace(config.STATE_DIR / mcpout.POLICY)
    mcpout._policy = None
    _prune_saved()
    return pol


def _prune_saved(days=14):
    try:
        d = mcpout.out_dir()
        cut = time.time() - days * 86400
        for f in d.iterdir():
            if f.stat().st_mtime < cut:
                f.unlink()
    except OSError:
        pass


# ---- report ------------------------------------------------------------------------------------------------
def summary(days=7, now=None):
    """[(key, line)] one waste line per tool, heaviest first."""
    now = now or time.time()
    pol = mcpout.policy()
    calls = [r for r in load_calls() if r["t"] >= now - days * 86400]
    by_key = {}
    for r in calls:
        by_key.setdefault(r["key"], []).append(r)
    rows = []
    for key, recs in by_key.items():
        e = pol.get(key) or {}
        level = e.get("level", "headtail")
        entered = sum(r["entered"] for r in recs)
        used = sum(min(r["used"], r["entered"]) for r in recs)
        s = stats(recs, level)
        at_level = s.get("chars", entered)
        waste_now = s.get("waste", entered - used)
        live = [r for r in recs if "reread" in r]
        rows.append((entered - used, key, (
            f"{key.replace('mcp__', '')}: {len(recs)} calls, {entered // 1000}k chars entered context, "
            f"{(entered - used) / entered if entered else 0:.0%} unused; level {level} "
            f"(would put {at_level // 1000}k, {waste_now // 1000}k unused)"
            + (f"; saved files re-read {sum(r['reread'] for r in live)}/{len(live)}" if live else "")
            + (f"; last change {e['history'][-1]['t']}: {e['history'][-1]['from']}→{e['history'][-1]['to']}"
               if e.get("history") else ""))))
    return [(k, line) for _, k, line in sorted(rows, reverse=True)]


def heavy_untightenable(days=7, now=None):
    """A tool wasting heavily at the tightest level it can take, for the one-line SessionStart notice."""
    now = now or time.time()
    pol = mcpout.policy()
    calls = [r for r in load_calls() if r["t"] >= now - days * 86400]
    worst = None
    for key, e in pol.items():
        if mcpout.passes_through(key.split("__")[1] if key.count("__") >= 2 else ""):
            continue  # left untouched on purpose (e.g. Roblox_Studio): nothing to tighten
        recs = [r for r in calls if r["key"] == key]
        if len(recs) < MIN_N:
            continue
        kind = key.split("#")[1] if "#" in key else e.get("kind", "read")
        level = e.get("level", "headtail")
        s = stats(recs, level)
        if s["n"] < MIN_N or s["waste"] < HEAVY_WEEK_CHARS or s["waste_ratio"] < 0.6:
            continue
        stuck = level == mcpout.MAX_LEVEL.get(kind) or decide(recs, level, kind)[0] == level
        if stuck and (worst is None or s["waste"] > worst[1]):
            worst = (key, s["waste"])
    return worst


def upkeep():
    """Background, at most once a day (started by SessionStart): track waste; remove the studio.md agent profile
    earlier versions installed (oof owns the Studio agent now); keep the Studio tools' preload setting applied."""
    try:
        track()
    except Exception as e:
        (config.STATE_DIR / "background.log").open("a").write(f"mcpwaste: track failed: {type(e).__name__}: {e}\n")
    from . import adapters
    try:
        from .adapters import roblox_studio
        roblox_studio.remove_agent_profile()
        if "roblox_studio" in adapters.enabled():
            from .adapters.roblox_studio import preload
            preload.upkeep()
    except Exception as e:
        (config.STATE_DIR / "background.log").open("a").write(f"mcpwaste: studio upkeep: {e}\n")


if __name__ == "__main__":
    upkeep()
