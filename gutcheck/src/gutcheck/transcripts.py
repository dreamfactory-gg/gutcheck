"""Read Claude Code transcripts (~/.claude/projects/**/*.jsonl) into simple event streams."""
import json
import os
from pathlib import Path

PROJECTS = Path(os.environ.get("GUTCHECK_PROJECTS_DIR", Path.home() / ".claude" / "projects"))

REJECT_MARKERS = ("The user doesn't want to proceed with this tool use", "User rejected tool use")
CLASSIFIER_MARKER = "Permission for this action was denied by the Claude Code auto mode classifier"
INTERRUPT_MARKER = "[Request interrupted by user"


def files(since=0.0, include_subagents=True):
    for p in PROJECTS.rglob("*.jsonl"):
        if not include_subagents and p.parent.parent != PROJECTS:
            continue
        try:
            if p.stat().st_mtime > since:
                yield p
        except OSError:
            pass


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content
                         if not isinstance(b, dict) or b.get("type") == "text")
    return ""


def events(path):
    """Yield dicts in order: {kind: prompt|text|tool|result, ...}. Results carry tool_use_id."""
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if t not in ("user", "assistant"):
                continue
            ts = d.get("timestamp")
            cwd = d.get("cwd")
            m = d.get("message") or {}
            c = m.get("content")
            if t == "user":
                if isinstance(c, str):
                    yield {"kind": "prompt", "text": c, "ts": ts, "meta": d.get("isMeta", False)}
                    continue
                for b in c or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        yield {"kind": "prompt", "text": b.get("text", ""), "ts": ts, "meta": d.get("isMeta", False)}
                    elif b.get("type") == "tool_result":
                        yield {"kind": "result", "id": b.get("tool_use_id"), "text": _text(b.get("content")),
                               "error": bool(b.get("is_error")), "ts": ts}
            else:
                for b in c or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        yield {"kind": "text", "text": b.get("text", ""), "ts": ts}
                    elif b.get("type") == "tool_use":
                        yield {"kind": "tool", "id": b.get("id"), "name": b.get("name"),
                               "input": b.get("input") or {}, "ts": ts, "cwd": cwd}


def is_human_prompt(ev):
    """A prompt typed by the owner (not a command caveat, system reminder or tool plumbing)."""
    if ev["kind"] != "prompt" or ev.get("meta"):
        return False
    t = ev["text"].lstrip()
    return bool(t) and not t.startswith(("<local-command", "<command-", "<system-reminder", "Caveat:",
                                         "<task-notification", "[Request interrupted"))


def bash_commands(path):
    """Yield {cmd, result, error, outcome, next_prompt, file, ts} for every Bash call in a transcript.

    outcome: ok | error | rejected (owner declined) | classifier (auto-mode denial) | interrupted
    next_prompt: the owner's next typed prompt, if it came before the next Bash call.
    """
    pending, since_prompt, out = {}, [], []
    for ev in events(path):
        if ev["kind"] == "tool" and ev["name"] == "Bash":
            rec = {"cmd": ev["input"].get("command", ""), "desc": ev["input"].get("description", ""),
                   "file": str(path), "ts": ev["ts"], "cwd": ev.get("cwd"), "outcome": None, "result": "", "next_prompt": None}
            pending[ev["id"]] = rec
            since_prompt.append(rec)
            out.append(rec)
        elif ev["kind"] == "result" and ev["id"] in pending:
            rec = pending.pop(ev["id"])
            txt = ev["text"]
            rec["result"] = txt[:2000]
            if any(m in txt for m in REJECT_MARKERS):
                rec["outcome"] = "rejected"
            elif CLASSIFIER_MARKER in txt:
                rec["outcome"] = "classifier"
            elif INTERRUPT_MARKER in txt:
                rec["outcome"] = "interrupted"
            else:
                rec["outcome"] = "error" if ev["error"] else "ok"
        elif ev["kind"] == "prompt" and (is_human_prompt(ev) or ev["text"].startswith("[Request interrupted")):
            for rec in since_prompt:
                rec["next_prompt"] = ev["text"][:500]
            since_prompt = []
    return out


def session_text(path, max_bytes=400_000, before_ts=None):
    """Earlier Bash commands and their outputs in this session (tail of the transcript), as one string.

    Used to recognise the agent's own jobs (a kill whose target this session started)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    parts = []
    for line in data.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if before_ts and d.get("timestamp") and d["timestamp"] >= before_ts:
            break
        m = d.get("message") or {}
        for b in m.get("content") or [] if isinstance(m.get("content"), list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "Bash":
                parts.append((b.get("input") or {}).get("command", ""))
            elif b.get("type") == "tool_result":
                parts.append(_text(b.get("content"))[:4000])
    return "\n".join(parts)
