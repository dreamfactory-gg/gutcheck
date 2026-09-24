"""Persistent soldiers, for any subagent: reuse a helper while it's warm, retire it with a handoff note, and start
its replacement from that note. The guidance the orchestrator follows is the `soldiers` skill; this module is the
plumbing behind it.

  PreToolUse SendMessage (main session): resuming an agent past its thresholds (context size, idle minutes) gets
      one line of additionalContext suggesting retirement. It never blocks.
  SubagentStop: a reply whose first line is NOTE_MARK is saved as that agent type's handoff note for this project.
  SubagentStart: a new agent of the same type in the same project gets the pending note once (then it's used).

Thresholds live in config: [soldier] max_context_tokens / max_idle_minutes, overridden per agent name or type in
[soldier.agents.<name>]. The defaults are conservative placeholders; oof's eval (soldier reuse vs fresh helper)
sets them.

  gutcheck soldier-note [AGENT_TYPE] [--path] [--dir D]   the latest note for this project (or its path)
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from . import config

NOTE_MARK = "HANDOFF NOTE"
MAX_NOTE_CHARS = 6000
RETIRE_MSG = (f"retire: reply with your handoff note only. First line exactly `{NOTE_MARK}`, then under 30 lines: "
              f"what you learned, the files (or scripts) and line ranges involved, what's done and what's open.")


def _key(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "-", s or "agent")


def note_path(cwd, agent_type):
    """One note per project directory and agent type, in gutcheck's state dir (never inside the project)."""
    try:
        root = Path(cwd or ".").expanduser().resolve()
    except OSError:
        root = Path(cwd or ".")
    return config.STATE_DIR / "handoffs" / re.sub(r"[^A-Za-z0-9]", "-", str(root)) / f"{_key(agent_type)}.md"


def default_idle_minutes():
    """Idle limit that matches the subagent prompt cache: 5 minutes by default in Claude Code, an hour only when
    CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL=1h (or an agent profile sets experimental cacheTtl: 1h; set a per-agent
    max_idle_minutes for those). Resuming after the cache expires re-sends the whole context at write price."""
    ttl = os.environ.get("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL", "").strip().lower()
    return 55 if ttl in ("1h", "60m", "3600", "3600s") else 4


def policy(name=None, agent_type=None):
    """Per-type soldier policy from enabled adapters, then user config [soldier.agents.<name|type>].
    Keys: reuse (bool, default True), fresh_above_main_tokens (int or None)."""
    pol = {"reuse": True, "fresh_above_main_tokens": None, "max_idle_minutes": None, "max_context_tokens": None}
    try:
        from . import adapters
        for mod in adapters.enabled():
            ad = adapters.for_server(next((s for s, m in adapters.KNOWN.items() if m == mod), ""))
            for pat, p in (getattr(ad, "SOLDIER_POLICIES", {}) or {}).items():
                if any(k and re.search(pat, k, re.I) for k in (agent_type, name)):
                    pol.update(p)
    except Exception:
        pass
    per = config.load().get("soldier", {}).get("agents") or {}
    for k in (agent_type, name):
        if k and isinstance(per.get(k), dict):
            pol.update({kk: v for kk, v in per[k].items() if kk in pol})
    return pol


def thresholds(name=None, agent_type=None):
    cfg = config.load().get("soldier", {})
    t = {"max_context_tokens": cfg.get("max_context_tokens", 80_000),
         "max_idle_minutes": cfg.get("max_idle_minutes", default_idle_minutes())}
    pol = policy(name, agent_type)  # adapter policies (e.g. Studio) set their own thresholds
    t.update({k: pol[k] for k in t if pol.get(k) is not None})
    per = cfg.get("agents") or {}
    for k in (agent_type, name):  # the name wins over the type
        if k and isinstance(per.get(k), dict):
            t.update({kk: v for kk, v in per[k].items() if kk in t})
    return t


# ---- SubagentStop: save the note -------------------------------------------------------------------------------
def capture(inp):
    """Save a retiring agent's note (its reply starts with NOTE_MARK). Returns the path written, or None."""
    text = inp.get("last_assistant_message") or ""
    if not text:
        from .evidence import last_assistant_text
        text = last_assistant_text(inp.get("agent_transcript_path") or "") or ""
    body = text.strip().strip("`").strip()
    if not body.startswith(NOTE_MARK):
        return None
    atype = inp.get("agent_type") or "agent"
    p = note_path(inp.get("cwd"), atype)
    p.parent.mkdir(parents=True, exist_ok=True)
    head = json.dumps({"t": time.strftime("%Y-%m-%d %H:%M"), "agent_type": atype, "agent_id": inp.get("agent_id"),
                       "session": inp.get("session_id"), "pending": True})
    p.write_text(f"<!-- {head} -->\n{body[:MAX_NOTE_CHARS]}\n")
    return p


def _split(text):
    m = re.match(r"<!-- (\{.*?\}) -->\n", text)
    if not m:
        return {}, text
    try:
        return json.loads(m.group(1)), text[m.end():]
    except ValueError:
        return {}, text[m.end():]


# ---- SubagentStart: hand the note to the replacement ----------------------------------------------------------
def start_context(inp):
    """additionalContext for a new agent: the pending note its retired predecessor (same type, same project) left,
    handed over once. None when there's no pending note."""
    atype = inp.get("agent_type")
    if not atype:
        return None
    p = note_path(inp.get("cwd"), atype)
    try:
        meta, note = _split(p.read_text())
    except OSError:
        return None
    if not meta.get("pending") or not note.strip():
        return None
    meta.update(pending=False, used_by=inp.get("agent_id"))
    p.write_text(f"<!-- {json.dumps(meta)} -->\n{note}")
    return (f"Handoff note from the previous {atype} agent in this project ({meta.get('t')}; saved at {p}). If your "
            f"brief continues that work, start from it and re-check anything that may have changed since; if it's "
            f"unrelated, ignore it.\n{note.strip()}")


# ---- PreToolUse SendMessage: suggest retiring instead of resuming ---------------------------------------------
def _target(inp, to):
    """(transcript, meta) of this session's subagent addressed by `to` (its name or id), newest first."""
    tp = inp.get("transcript_path") or ""
    if not tp or not to:
        return None, None
    sub = Path(tp).with_suffix("") / "subagents"
    best = None
    for meta in sub.glob("agent-*.meta.json") if sub.is_dir() else []:
        aid = meta.name[len("agent-"):-len(".meta.json")]
        try:
            m = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        if to not in (m.get("name"), aid, f"agent-{aid}") and not (len(to) >= 6 and aid.startswith(to)):
            continue
        f = meta.with_name(f"agent-{aid}.jsonl")
        if f.exists() and (best is None or f.stat().st_mtime > best[0].stat().st_mtime):
            best = (f, m)
    return best or (None, None)


def check_send(inp, now=None):
    """One line of additionalContext when the agent being resumed is past its thresholds, else None."""
    if inp.get("agent_id") or config.load().get("soldier", {}).get("mode", "on") == "off":
        return None
    ti = inp.get("tool_input") or {}
    msg = ti.get("message")
    if not isinstance(msg, str) or msg.lstrip().lower().startswith("retire"):
        return None  # structured messages (shutdown etc.) and retirements go through without comment
    to = str(ti.get("to") or "")
    f, meta = _target(inp, to)
    if f is None:
        return None
    pol = policy(meta.get("name"), meta.get("agentType"))
    if not pol["reuse"]:
        return {"additionalContext": (
            f"gutcheck: don't resume {to}: {meta.get('agentType') or 'this'} agents aren't reused (a SendMessage resume "
            f"runs in the background and costs the main session extra turns). Start a fresh "
            f"{meta.get('agentType') or 'helper'} agent in the foreground with a short brief for this task."),
            "_rec": {"to": to, "agent_type": meta.get("agentType"), "policy": "no-reuse"}}
    from .hooks import context_tokens
    t = thresholds(meta.get("name"), meta.get("agentType"))
    ctx = context_tokens(str(f)) or 0
    idle = ((now or time.time()) - f.stat().st_mtime) / 60
    why = []
    if ctx > t["max_context_tokens"]:
        why.append(f"its context is ~{ctx // 1000}k tokens")
    if idle > t["max_idle_minutes"]:
        why.append(f"it has been idle {int(idle)} min (cache cold)")
    if not why:
        return None
    return {"additionalContext": (
        f"gutcheck: {to} is past its reuse thresholds ({' and '.join(why)}). If the rest of this work can start "
        f"fresh, retire it instead: SendMessage it \"{RETIRE_MSG}\", then start a new {meta.get('agentType')} "
        f"agent with your brief; it gets the note automatically."),
        "_rec": {"to": to, "agent_type": meta.get("agentType"), "context_tokens": ctx, "idle_min": round(idle, 1)}}


def cli(args):
    import argparse
    ap = argparse.ArgumentParser(prog="gutcheck soldier-note")
    ap.add_argument("agent_type", nargs="?", help="agent type (default: the most recent note in this project)")
    ap.add_argument("--path", action="store_true", help="print only the note's path")
    ap.add_argument("--dir", default=".", help="project directory (default: the current one)")
    a = ap.parse_args(args)
    if a.agent_type:
        p = note_path(a.dir, a.agent_type)
    else:
        d = note_path(a.dir, "x").parent
        notes = sorted(d.glob("*.md"), key=lambda f: f.stat().st_mtime) if d.is_dir() else []
        p = notes[-1] if notes else None
    if a.path:
        print(p or note_path(a.dir, "<agent-type>"))
        return
    try:
        meta, note = _split(p.read_text())
    except (OSError, AttributeError):
        print(f"no handoff note for {Path(a.dir).resolve()}" + (f" ({a.agent_type})" if a.agent_type else ""))
        sys.exit(1)
    print(f"{p} ({meta.get('agent_type')}, {meta.get('t')}, {'pending' if meta.get('pending') else 'used'})")
    print(note.rstrip())
