"""Durable transitions: what we're working on survives a compaction, a /clear and a restart.

The session keeps a `## Now` block at the top of its TASKS.md (goal, item in progress, next step, the owner's latest
ask, running background jobs); the `handoff` skill tidies it before a planned restart. At each transition
(PreCompact, SessionEnd) tokenmaxxing, on the TASKS.md this session read or wrote:
  - moves ticked `- [x]` items to REPORT.md next to it, under a dated heading (open items are never touched);
  - backs up the Now block when it's missing or stale. Stale = TASKS.md not modified since the owner's last prompt
    in this session. The backup is one tool-less `claude -p` turn on Opus at low effort over the transcript tail,
    written into TASKS.md (marked as tokenmaxxing's) and into the checkpoint, which puts the Now block first. On exit it
    runs only for a session that keeps a TASKS.md (a restart may never come); compaction and /clear always continue.

Live sessions and each project's last ended session are kept in <state>/sessions.json (SessionStart adds a live
entry with Claude Code's pid, SessionEnd removes it and records the ended session before its checkpoint is written;
an entry is dead once its pid is gone or it is older than LIVE_TTL). SessionStart re-injects:
  compact  the same session's checkpoint (checkpoint.for_session_start);
  clear    the checkpoint SessionEnd(reason "clear") wrote just before, in the hook itself;
  startup  the project's last ended session's checkpoint, if it ended under PICKUP_MAX ago and no other session is
           live in the project. SessionEnd on exit detaches the slow part; a startup right after
           waits up to READY_WAIT for it, else gets a pointer to where the checkpoint will be.
  resume   nothing: the context is already there.
A session with no owner prompt leaves the last ended record alone. config [transitions] mode = "on" | "off".
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from gutcheck_core import config
from gutcheck_core.lock import locked

from . import checkpoint
from .stopcheck import DONE_RE, OPEN_RE

LIVE_TTL = 24 * 3600
PICKUP_MAX = 6 * 3600
CLEAR_MAX = 600
READY_WAIT = 4.0
JUST_ENDED = 120
TAIL_CHARS = 24_000
NOW_MAX_CHARS, NOW_MAX_LINES = 1500, 40
NOW_RE = re.compile(r"^## Now[ \t]*\r?$", re.MULTILINE)
HEADING_RE = re.compile(r"^#{1,6}(?:[ \t]|\r?$)", re.MULTILINE)
FENCE_RE = re.compile(r"^\s*(```|~~~)")
TASK_RE = re.compile(r"^\s*[-*+] \[[ xX]\]")
SHELLS = {"sh", "bash", "zsh", "dash", "fish", "tokenmaxxing-hook"}

NOW_SYSTEM = "You summarise where a coding session stands, for the session that continues it. Answer in plain text."
NOW_PROMPT = """Below is the end of a Claude Code session: the owner's messages and the agent's replies, then its running background jobs and open task items. Write the session's Now block: exactly these five lines, one short sentence each, nothing before or after:
- Goal: <what the session is working towards>
- In progress: <the item being worked on right now>
- Next step: <the concrete next action>
- Owner's latest ask: <the owner's most recent request, close to their words>
- Running jobs: <ids and what each does, or "none">
Say only what the text supports; write "unclear" rather than guess.

"""


def _on():
    """Interactive sessions only: headless `claude -p` runs (benches, harnesses, tokenmaxxing's own calls) have no
    owner to hand work back to, and would each pay for a backup call."""
    ep = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
    return (config.load().get("transitions", {}).get("mode", "on") != "off" and not os.environ.get("GUTCHECK_NESTED")
            and not ep.startswith("sdk") and ep not in ("bench", "local-agent"))


def _read(path, newline=None):
    try:
        with open(path, errors="replace", newline=newline) as f:
            return f.read()
    except OSError:
        return ""


def _put(path, text):
    """Atomic write, newlines as given. `path` should be resolved, so a symlink is written through, not replaced."""
    tmp = Path(f"{path}.{os.getpid()}.tmp")
    with open(tmp, "w", newline="") as f:
        f.write(text)
    tmp.replace(path)


def _epoch(ts):
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def _now_span(text):
    """(start, end) of the Now block: its heading up to the next heading or task item outside a fence, or the end
    of the file. Stopping at task items keeps open items that sit under the Now heading out of a rewrite."""
    m = NOW_RE.search(text)
    if not m:
        return None
    pos, fence = m.end(), None
    for ln in text[m.end():].splitlines(keepends=True):
        f = FENCE_RE.match(ln)
        if f:
            fence = None if fence == f.group(1) else fence or f.group(1)
        elif not fence and (HEADING_RE.match(ln) or TASK_RE.match(ln)):
            break
        pos += len(ln)
    return m.start(), pos


def now_block(path):
    """The Now block, cut to NOW_MAX_CHARS / NOW_MAX_LINES (with a pointer to TASKS.md), or None."""
    text = _read(path) if path else ""
    span = _now_span(text)
    if not span:
        return None
    block = text[span[0]:span[1]].strip()
    lines = block.splitlines()[:NOW_MAX_LINES]
    while len("\n".join(lines)) > NOW_MAX_CHARS:
        lines.pop()
    cut = "\n".join(lines)
    return block if cut == block else f"{cut}\n- (Now block cut to fit; the rest is in {path})"


def _indent(ln):
    return len(ln) - len(ln.lstrip())


def move_ticked(path, today=None):
    """Move ticked items (with their indented detail lines, blank lines inside included) from TASKS.md to REPORT.md
    beside it; lines in code fences are left alone. A ticked item with an open item under it stays. Keeps the file's
    newline style. Returns the number of items moved."""
    path = Path(path).resolve()
    text = _read(path, newline="")
    lines = text.splitlines(keepends=True)
    nl = "\r\n" if "\r\n" in text else "\n"
    fenced, fence = [], None
    for ln in lines:
        m = FENCE_RE.match(ln)
        fenced.append(bool(fence or m))
        if m and (not fence or m.group(1) == fence):
            fence = None if fence else m.group(1)
    keep, moved, n, i = [], [], 0, 0
    while i < len(lines):
        j, ind = i + 1, _indent(lines[i])
        if not fenced[i] and DONE_RE.match(lines[i]):
            while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > ind):
                j += 1
            while not lines[j - 1].strip():  # trailing blank lines stay in TASKS.md
                j -= 1
            if not any(OPEN_RE.match(x) and not f for x, f in zip(lines[i + 1:j], fenced[i + 1:j])):
                moved += lines[i:j]
                n += 1
                i = j
                continue
        keep += lines[i:j]
        i = j
    if not n:
        return 0
    report = path.with_name("REPORT.md").resolve()
    old = _read(report, newline="")
    heading = f"## {today or time.strftime('%Y-%m-%d')} (moved from TASKS.md)"
    last = ([ln for ln in old.splitlines() if ln.startswith("## ")] or [""])[-1]
    add = "".join(moved) if last == heading else f"{nl}{heading}{nl}" + "".join(moved)
    _put(report, (old or f"# Report{nl}").rstrip("\r\n") + nl + add.rstrip("\r\n") + nl)
    new = "".join(keep)
    _put(path, new if text.endswith(("\n", "\r")) else new.rstrip("\r\n"))
    return n


def _tail(s):
    """Owner messages and agent replies, newest last, within TAIL_CHARS."""
    msgs = sorted([(m["ts"] or "", "OWNER", m["text"]) for m in s["owner"] if m["src"] == "owner"]
                  + [(a["ts"] or "", "AGENT", a["text"]) for a in s["agent_text"]])
    out, n = [], 0
    for ts, who, text in reversed(msgs):
        line = f"[{who} {checkpoint._fmt_ts(ts)}] {checkpoint._short(text, 2000)}"
        n += len(line) + 1
        if n > TAIL_CHARS:
            break
        out.append(line)
    return "\n".join(reversed(out))


def write_now(s, tasks, timeout=40):
    """The backup writer: one Opus low-effort turn over the transcript tail -> the Now block (also written into
    TASKS.md when there is one, replacing the old block whole). Returns (block, cost meta)."""
    from gutcheck_core import secretscan

    from . import evidence, stopcheck
    jobs = [f"- {v['kind']} `{k}`: {checkpoint._short(v.get('desc') or '', 110)}" for k, v in checkpoint.running(s)]
    items = stopcheck.task_items(Path(tasks))[0] if tasks else []
    prompt = (NOW_PROMPT + "## Transcript tail\n" + _tail(s) + "\n\n## Running background jobs\n"
              + ("\n".join(jobs) or "none") + "\n\n## Open items\n" + ("\n".join(f"- [ ] {x}" for x in items) or "none"))
    text, meta = evidence._claude(secretscan.redact(prompt), "opus", "low", timeout=timeout, system=NOW_SYSTEM)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("- ")]
    if not lines:
        raise ValueError(f"no Now lines in reply: {text[:200]}")
    block = (f"## Now\n_Written by tokenmaxxing from the transcript, {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} "
             "UTC: the session hadn't kept this block current._\n" + "\n".join(lines))
    if tasks:
        path = Path(tasks).resolve()
        old = _read(path, newline="")
        nl = "\r\n" if "\r\n" in old else "\n"
        title = re.match(r"# .*(?:\r?\n)+", old)
        start, end = _now_span(old) or ((title.end() if title else 0),) * 2
        _put(path, old[:start] + (block + "\n\n").replace("\n", nl) + old[end:].lstrip("\r\n"))
    return block, meta


def prepare(inp, s):
    """Before a checkpoint: move ticked items, then return the Now block (backed up when missing or stale), or None.
    Fails open."""
    from gutcheck_core.hookio import _safe, log

    from .stopcheck import find_tasks
    if not _on():
        return None
    # only the TASKS.md this session read or wrote: one it never touched belongs to some other run
    tasks = find_tasks(inp.get("cwd") or s["cwd"], inp.get("transcript_path"))[0]
    last = next((_epoch(m["ts"]) for m in reversed(s["owner"]) if m["src"] == "owner" and m["ts"]), None)
    stale = bool(tasks and last and os.path.getmtime(tasks) < last)  # before move_ticked bumps the mtime
    if tasks:
        _safe("transitions: move ticked", move_ticked, tasks)
    now = now_block(tasks)
    clear = inp.get("reason") == "clear"
    exiting = inp.get("hook_event_name") == "SessionEnd" and not clear
    # an exit may never be followed by a restart: no model call unless the session keeps a TASKS.md
    if (now and not stale) or (exiting and not tasks):
        return now
    res = _safe("transitions: Now backup", write_now, s, tasks, 12 if clear else 40)
    log("now_backup", {"session": inp.get("session_id"), "tasks": tasks and str(tasks), "had_now": bool(now), "ok": bool(res),
                       **(res[1] if res else {})})
    return res[0] if res else now


# ---- live sessions and pickup -------------------------------------------------------------------------------
def project(cwd):
    return (cwd and checkpoint.git_root(cwd)) or cwd or ""


def _sessions(fn):
    """fn(state) under a lock on <state>/sessions.json; state is written back after. Returns fn's result."""
    p = config.STATE_DIR / "sessions.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_suffix(".lock"), "a") as lk, locked(lk):
        try:
            st = json.loads(_read(p) or "{}")
        except ValueError:
            st = {}
        st = st if isinstance(st, dict) else {}
        st = {k: st[k] if isinstance(st.get(k), dict) else {} for k in ("live", "ended")}
        out = fn(st)
        _put(p, json.dumps(st))
    return out


def _claude_pid():
    """Claude Code's pid: the nearest ancestor that isn't a shell (hooks run under `sh -c` and the shim)."""
    pid = os.getppid()
    for _ in range(4):
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, check=False,
                             timeout=3).stdout.split(None, 1)
        if len(out) < 2 or os.path.basename(out[1].strip()) not in SHELLS:
            break
        pid = int(out[0])
    return pid


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError):  # someone else's process; no pid recorded
        pass
    return True


def pickup(source, ended, others, t):
    """The label to re-inject the last ended session's checkpoint under, or None."""
    if not isinstance(ended, dict) or not {"session", "t", "checkpoint"} <= ended.keys():
        return None
    if source == "clear" and ended.get("reason") == "clear" and t - ended["t"] < CLEAR_MAX:
        return "/clear"
    if source == "startup" and not others and t - ended["t"] < PICKUP_MAX:
        name = f"{ended['name']}, " if ended.get("name") else ""
        return f"previous session {name}{ended['session']}, ended {int((t - ended['t']) // 60)} min ago"
    return None


def on_start(inp):
    """SessionStart: register the session as live and return the checkpoint text to re-inject, or None."""
    from gutcheck_core.hookio import _safe
    if not _on():
        return checkpoint.for_session_start(inp)
    sid, source, t = inp.get("session_id"), inp.get("source"), time.time()
    proj, pid = project(inp.get("cwd")), _safe("transitions: claude pid", _claude_pid)

    def register(st):
        st["live"] = {k: v for k, v in st["live"].items()
                      if isinstance(v, dict) and t - v.get("t", 0) < LIVE_TTL and _alive(v.get("pid"))}
        others = [k for k, v in st["live"].items() if v.get("project") == proj and k != sid]
        st["live"][sid] = {"project": proj, "t": t, "pid": pid}
        return st["ended"].get(proj), others

    # the compaction re-injection must not depend on sessions.json
    reg = _safe("transitions: register", _sessions, register)
    if source == "compact":
        return checkpoint.for_session_start(inp)
    label = reg and pickup(source, *reg, t)
    if not label:
        return None
    ended, cp = reg[0], Path(reg[0]["checkpoint"])

    def ready():  # SessionEnd records the session before its checkpoint is written
        return cp.exists() and cp.stat().st_mtime >= ended["t"]

    just = t - ended["t"] < JUST_ENDED
    while just and not ready() and time.time() - t < READY_WAIT:
        time.sleep(0.2)
    if not ready():
        return (f"# WHERE WE ARE\nPrevious session {ended['session']} ended just now; its checkpoint will be at {cp} "
                "(read it before starting).") if just else None
    from gutcheck_core import rules
    with rules._locked(sid):  # carry the standing rules over, so the next checkpoint still has them
        if not rules.load(sid):
            rules._save(sid, rules.load(ended["session"]))
    text = _read(ended["checkpoint"]).split("\n", 1)[-1]
    return f"# WHERE WE ARE (from {label})\n{text.rstrip()}"


def on_end(inp):
    """SessionEnd: drop the live entry and record the session as its project's last ended one (a startup right after
    must see it), then checkpoint: in the hook on /clear, else detached."""
    from gutcheck_core.hookio import _safe
    if not _on():
        return
    sid = inp.get("session_id")
    s = checkpoint.scan(inp.get("transcript_path") or "")
    owned = any(m["src"] == "owner" for m in s["owner"])
    rec = {"session": sid, "name": checkpoint.session_name(s), "reason": inp.get("reason"), "t": time.time(),
           "checkpoint": str(checkpoint.paths_for(sid)[0])}

    def end(st):
        st["live"].pop(sid, None)
        if owned:
            st["ended"][project(inp.get("cwd") or s["cwd"])] = rec

    _safe("transitions: end", _sessions, end)
    if not owned:
        return
    if inp.get("reason") == "clear":
        finish(inp, s)
    else:
        from .health import _bg
        _bg([json.dumps(inp)], module="tokenmaxxing.transitions")


def finish(inp, s=None):
    """Write the ended session's checkpoint."""
    clear = inp.get("reason") == "clear"
    checkpoint.write(inp, deadline=6.0 if clear else checkpoint.JEV_DEADLINE,
                     source="/clear" if clear else "session end", s=s)


if __name__ == "__main__":  # the detached SessionEnd worker (health._bg)
    from gutcheck_core.hookio import _safe
    _safe("SessionEnd: finish", finish, json.loads(sys.argv[1]))
