"""Context kept out by tokenmaxxing and oof, times the later API calls that would have re-sent it (until compaction).
Re-sends are priced as cache reads (0.1x); tokens ~ chars / 4."""
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from gutcheck_core import config, transcripts

OOF_HOME = Path(os.environ.get("OOF_HOME", Path.home() / ".cache" / "oof"))
CACHE_READ = 0.1
STUDIO = "mcp__Roblox_Studio__"


def _jsonl(path):
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def _epoch(iso):
    try:
        return time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0.0


def kept_events(since):
    calls = [d for d in _jsonl(config.STATE_DIR / "mcp_calls.jsonl") if (d.get("t") or 0) >= since]
    out = []
    for d in calls:
        k = (d.get("full") or 0) - (d.get("entered") or 0)
        if k > 0:
            out.append({"id": d.get("id"), "tool": d.get("tool"), "source": "tokenmaxxing", "kept": k})
    studio = [d for d in calls if str(d.get("tool", "")).startswith(STUDIO)]
    taken = set()
    for r in _jsonl(OOF_HOME / "metrics.jsonl"):
        if "studio_out" not in r:
            continue
        t = _epoch(r.get("t"))
        if t < since:
            continue
        k = r["studio_out"] - r["out"]
        if k <= 0:
            continue
        best = None
        for d in studio:
            if d["id"] in taken or d.get("tool") != STUDIO + r["tool"] or abs(d["t"] - t) > 15:
                continue
            if abs((d.get("entered") or 0) - r["out"]) > max(64, r["out"] // 20):
                continue
            if best is None or abs(d["t"] - t) < abs(best["t"] - t):
                best = d
        if best:
            taken.add(best["id"])
        out.append({"id": best and best["id"], "tool": STUDIO + r["tool"], "source": "oof", "kept": k})
    return out


def later_calls(ids, since):
    want = set(filter(None, ids))
    found = {}
    if not want:
        return found
    for p in transcripts.files(since=since):
        try:
            raw = p.read_text(errors="replace")
        except OSError:
            continue
        if not any(i in raw for i in want):
            continue
        reqs, marks, seen = 0, [], set()
        for ln in raw.splitlines():
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if d.get("type") == "assistant":
                rid = d.get("requestId") or (d.get("message") or {}).get("id")
                if rid and rid not in seen:
                    seen.add(rid)
                    reqs += 1
            elif d.get("type") == "system" and d.get("subtype") == "compact_boundary":
                marks.append(("|", reqs))
            elif d.get("type") == "user":
                c = (d.get("message") or {}).get("content")
                for b in c if isinstance(c, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in want:
                        marks.append((b["tool_use_id"], reqs))
        for i, (tid, pos) in enumerate(marks):
            if tid == "|":
                continue
            end = next((q for t2, q in marks[i + 1:] if t2 == "|"), reqs)
            found[tid] = (p.stem, max(end - pos, 0))
    return found


def compute(days=7):
    since = time.time() - days * 86400
    ev = kept_events(since)
    later = later_calls([e["id"] for e in ev], since)
    per = defaultdict(lambda: defaultdict(int))
    per_session = defaultdict(int)
    tot = defaultdict(int)
    for e in ev:
        sess, n = later.get(e["id"], (None, None))
        k = per[(e["source"], e["tool"])]
        k["calls"] += 1
        k["kept"] += e["kept"]
        tot["calls"] += 1
        tot["kept"] += e["kept"]
        if n is None:
            tot["unmatched"] += 1
            tot["unmatched_kept"] += e["kept"]
            k["cost"] += e["kept"]
            tot["cost"] += e["kept"]
            continue
        k["carried"] += e["kept"] * n
        k["cost"] += e["kept"] * (1 + CACHE_READ * n)
        tot["carried"] += e["kept"] * n
        tot["cost"] += e["kept"] * (1 + CACHE_READ * n)
        tot["later"] += n
        per_session[sess] += e["kept"]
    return {"days": days, "tot": tot, "per": per, "per_session": per_session}


def tk(chars):
    t = chars / 4
    return f"{t / 1e6:,.2f}M" if t >= 1e6 else f"{t / 1e3:,.1f}k"


def lines(r):
    t = r["tot"]
    if not t["calls"]:
        return [f"Savings: nothing kept out of context in the last {r['days']} days"]
    matched = t["calls"] - t["unmatched"]
    out = [f"Savings (tokens ~ chars/4): {tk(t['kept'])} tokens kept out of context over {t['calls']} tool calls",
           f"  carried: re-sent on {t['later']:,} later API calls in their sessions = {tk(t['carried'])} tokens "
           f"not re-read (avg {t['later'] / max(matched, 1):.0f} later calls each)",
           f"  cost-equivalent: {tk(t['cost'])} input tokens at full price (re-sends priced as cache reads, "
           f"{CACHE_READ}x)"]
    if t["unmatched"]:
        out.append(f"  {t['unmatched']} calls ({tk(t['unmatched_kept'])} tokens) not found in a transcript: counted once")
    if r["per_session"]:
        out.append(f"  most kept out of one session's context: {tk(max(r['per_session'].values()))} tokens")
    out.append("  by tool (kept -> carried):")
    for (src, tool), k in sorted(r["per"].items(), key=lambda kv: -kv[1]["cost"])[:10]:
        out.append(f"    {src:8s} {tool.replace('mcp__', '')}: {k['calls']} calls, {tk(k['kept'])} -> {tk(k['carried'])}")
    return out


def main(args):
    days = int(args[0]) if args and args[0].isdigit() else 7
    print("\n".join(lines(compute(days))))
