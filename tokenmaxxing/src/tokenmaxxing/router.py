"""UserPromptSubmit context router (Phase 6): skill router + conditional doc/memory loading.

for_prompt(inp) -> additionalContext text, or None. Skill routing and doc/memory relevance run in parallel,
each bounded by its own Jev deadline; any failure means no output (fail open). Every decision is logged to
router.jsonl in the state dir. Nothing is suggested or loaded twice in one session.
"""
import json
import os
import re
import threading
import time
import traceback

from gutcheck_core import config

SKIP_PREFIXES = ("/", "<", "Another Claude session", "This session is being continued", "[Request interrupted")


def log(rec):
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.STATE_DIR / "router.jsonl", "a") as fh:
            fh.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")
    except Exception:
        pass


def skip(prompt, min_chars=3):
    p = (prompt or "").lstrip()
    return len(p) < min_chars or p.startswith(SKIP_PREFIXES)


def last_assistant_text(path, max_bytes=300_000):
    """The latest main-thread assistant text in the transcript (what the owner is replying to)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return ""
    for line in reversed(lines):
        if '"assistant"' not in line or '"isSidechain":true' in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        c = (d.get("message") or {}).get("content")
        if d.get("type") == "assistant" and isinstance(c, list):
            txt = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            if txt.strip():
                return txt
    return ""


# ---- per-session memory of what was already suggested or loaded -------------------------------------------
def _seen_path(session, kind):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session or "none")[:80]
    return config.STATE_DIR / "router_seen" / f"{safe}.{kind}.json"


def load_seen(session, kind):
    """Names (skills) or section keys (context) already suggested or loaded in this session."""
    try:
        return json.loads(_seen_path(session, kind).read_text())
    except (OSError, ValueError):
        return []


def save_seen(session, kind, items):
    try:
        p = _seen_path(session, kind)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(items))
    except OSError:
        pass


def prune_seen(days=7):
    """Drop per-session state older than `days` and background jobs that never ran."""
    cutoff = time.time() - days * 86400
    old = [p for sub in ("router_seen", "router_pending") for p in (config.STATE_DIR / sub).glob("*.json")]
    old += (config.STATE_DIR / "read_gate").glob("*")
    for p in old:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


# ---- the two routes ---------------------------------------------------------------------------------------------
def skill_route(prompt, context, session, deadline=None):
    """(line or None, log record)."""
    from . import skills
    st = skills.settings()
    rec = {"mode": st["mode"]}
    if st["mode"] == "off":
        return None, rec
    idx = skills.load_index().get("skills")
    if idx is None:
        _background(["skill-find", "--reindex"])
        rec["status"] = "not indexed"
        return None, rec
    cands = skills.router_candidates(idx)
    res = skills.rank(prompt, context, cands, deadline=deadline if deadline is not None else st["deadline_s"])
    name = skills.decide(res, st["min_prob"], st["min_fits"])
    rec.update(top=[(n, round(p, 3), round(f, 3), lit) for n, p, f, lit in res["ranked"][:3]], none=round(res["none"], 3),
               jev_ms=res["ms"], suggest=name)
    if not name:
        return None, rec
    seen = load_seen(session, "skills")
    if name in seen:
        rec["repeat"] = True
        return None, rec
    save_seen(session, "skills", seen + [name])
    path = {s["name"]: s["path"] for s in idx}[name]
    line = f"relevant skill: {name}, read {path}"
    return (line if st["mode"] == "on" else None), rec


def context_route(inp, prompt, context, deadline=None):
    """(text or None, log record). In log mode the work runs in a background process, off the prompt's path."""
    from . import ctxload
    st = ctxload.settings()
    rec = {"mode": st["mode"]}
    if st["mode"] == "off" or len(prompt.strip()) < st["min_prompt_chars"]:
        return None, rec
    if st["mode"] != "on" and not inp.get("_background"):
        pend = config.STATE_DIR / "router_pending"
        pend.mkdir(parents=True, exist_ok=True)
        f = pend / f"{time.time():.6f}-{os.getpid()}.json"
        f.write_text(json.dumps({"prompt": prompt, "context": context, "cwd": inp.get("cwd"),
                                 "session_id": inp.get("session_id")}))
        _background(["skill-find", "--context-log", str(f)])
        rec["status"] = "background"
        return None, rec
    session = inp.get("session_id")
    root = ctxload.project_root(inp.get("cwd") or os.getcwd())
    seen = load_seen(session, "sections")
    res = ctxload.route(root, prompt, context, deadline=deadline if deadline is not None else st["deadline_s"],
                        seen=set(seen))
    if res.get("status") == "not indexed" or res.get("stale"):
        _background(["skill-find", "--index-context", root])
    rec.update({k: v for k, v in res.items() if k != "sections"})
    fresh = [s for s in res.get("sections", []) if not s["repeat"]]
    rec["sections"] = [(s["key"].replace(os.path.expanduser("~"), "~"), s["p"], s["repeat"])
                       for s in res.get("sections", [])]
    if not fresh:
        return None, rec
    save_seen(session, "sections", seen + [s["key"] for s in fresh])
    return (ctxload.render(root, fresh, st) if st["mode"] == "on" else None), rec


def context_log(path):
    """Background half of log mode: route the saved prompt and log what would have been loaded."""
    try:
        job = json.loads(open(path).read())
        os.unlink(path)
    except (OSError, ValueError):
        return
    t0 = time.monotonic()
    try:
        _, rec = context_route(dict(job, _background=True), job["prompt"], job.get("context", ""), deadline=10)
    except Exception as e:
        rec = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    log({"session": job.get("session_id"), "background": True, "ms": int((time.monotonic() - t0) * 1000),
         "context": rec})


def _background(args):
    try:
        from . import health
        health._bg(args)
    except Exception:
        pass


# ---- entry point --------------------------------------------------------------------------------------------------
def for_prompt(inp):
    try:
        return _for_prompt(inp)
    except Exception as e:
        log({"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-800:]})
        return None


def _for_prompt(inp):
    prompt = inp.get("prompt") or ""
    if skip(prompt):
        return None
    t0 = time.monotonic()
    session = inp.get("session_id")
    context = last_assistant_text(inp.get("transcript_path"))
    results = {}

    def run(key, fn, *a):
        try:
            results[key] = fn(*a)
        except Exception as e:  # JevError included: fail open
            results[key] = (None, {"error": f"{type(e).__name__}: {str(e)[:200]}"})

    threads = [threading.Thread(target=run, args=("skill", skill_route, prompt, context, session), daemon=True),
               threading.Thread(target=run, args=("context", context_route, inp, prompt, context), daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=max(0.1, 2.5 - (time.monotonic() - t0)))
    skill_line, skill_rec = results.get("skill", (None, {"error": "timeout"}))
    ctx_text, ctx_rec = results.get("context", (None, {"error": "timeout"}))
    log({"session": session, "prompt_chars": len(prompt), "ms": int((time.monotonic() - t0) * 1000),
         "skill": skill_rec, "context": ctx_rec})
    out = "\n\n".join(x for x in (skill_line, ctx_text) if x)
    return out or None
