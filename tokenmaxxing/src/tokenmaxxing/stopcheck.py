"""Long-run stop enforcement (Phase 8). Returns hook output dict or None.

Active only when the session was started with CLAUDE_LONG_RUN=1 (the `claude` shell wrapper sets it; `short`
doesn't). At each end of turn:
  1. background work that will wake the session still running -> allow (not a stall);
  2. classify the final message with Jev (one request, several questions);
  3. legitimate stop (real blocker, blocking question) -> allow and notify; task done -> allow;
  4. early stop -> block, naming the open items from TASKS.md;
  5. the cap counts stalls without progress (TASKS.md ticks, or file/state-changing tool calls since the last nudge);
  6. at the cap: one BLOCKED nudge, then allow the stop, write STALLED.md and notify;
  7. every stop is logged to <state>/stops.jsonl.

config [stop]: mode = "log" (classify and log what it would do) | "on" (act); max_stalls; thresholds;
notify_cmd (optional argv list; the one-line message is appended) for push notifications.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from gutcheck_core import config

MARK = "[tokenmaxxing long-run]"
TERMINAL = {"completed", "complete", "done", "failed", "error", "errored", "killed", "stopped", "cancelled",
            "canceled", "exited", "finished", "succeeded", "success", "timeout", "timed_out", "idle"}
TM_RE = re.compile(r'<teammate-message teammate_id=\\?"([^"\\]+)\\?"')
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Agent", "Task", "Workflow"}
MCP_WRITE = re.compile(r"(write|create|update|set|delete|remove|insert|execute|exec|run|apply|move|rename|"
                       r"commit|push|send|post|put|patch|upload|install|edit|save|publish|deploy|start|stop)", re.I)
OPEN_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[ \]\s+(.+?)\s*$")
DONE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[[xX]\]\s+(.+?)\s*$")

# One Jev choice over how the turn ended. Replay eval (evals/stop_eval.py): a single choice separated the kinds
# better than one noul per kind, which fired "offer"/"blocker"/"check_back" together on long orchestration reports.
KINDS = {
    "announce": "The message ends by naming the next step of the work, which the assistant or an agent it directs "
                "could start now without the user, but it stops without starting it.",
    "offer": "The message offers to do a routine next step of the requested work, or asks permission for it ('want "
             "me to...?', 'say go and I'll...'), though the step is not risky and could go ahead without the "
             "user's answer.",
    "decisions": "The message hands the user decisions or options although, by its own account, none of them blocks "
                 "the remaining work the assistant could keep doing.",
    "milestone": "The message reports progress at a milestone and stops, while it, or `open_items`, lists remaining "
                 "work the assistant could do now without the user.",
    "check_back": "The message promises to check or report back later on a job or agent, but nothing is running or "
                  "scheduled that will wake the assistant.",
    "waiting_on_work": "The message is waiting on background work (an agent, a command, a job, a teammate) that is "
                       "still running and will report back; nothing else is left to do meanwhile.",
    "blocker": "The message stops on something only the user can do: a credential or access, a manual or physical "
               "action (run, play, test, click, paste output), approving spend, or sign-off on a risky or "
               "irreversible step such as a production deploy, a merge to a shared branch, deleting data or "
               "restarting a live service.",
    "question": "The message asks the user a question whose answer genuinely blocks all remaining work.",
    "done": "The message reports that the work the user asked for is finished, or answers the user's question; any "
            "extra suggestions are optional.",
}
INSTRUCTIONS = ("How does the assistant's turn end in `final_message`, given `user_request` and `open_items`? "
                "Pick the best description.")
EARLY = ("announce", "offer", "decisions", "milestone", "check_back")
LEGIT = ("blocker", "question")
DEFAULT_TH = {"early": 0.6}   # block when the early-stop kinds together hold at least this much probability

NUDGE = {
    "announce": "You ended the turn by announcing the next step instead of taking it. Take it now; put any status "
                "note in the same message as the tool call.",
    "offer": "You ended the turn by offering to continue. The owner wants the work done, not offered: carry on. "
             "If a step is risky or irreversible, say exactly what needs sign-off and continue with everything else.",
    "decisions": "You ended the turn on a list of decisions, none of which blocks the rest of the work. Put your "
                 "recommendations in the same message as your next tool call and carry on with what doesn't depend "
                 "on them.",
    "milestone": "You stopped to report at a milestone while work remains. Status notes are welcome in the same "
                 "message as your next tool call; carry on with the remaining work.",
    "check_back": "You said you'd check back, but nothing is scheduled to wake you. Wait for it in this turn (run the "
                  "check, or a blocking wait) or move on to other open work instead of ending the turn.",
}


# ---- inputs --------------------------------------------------------------------------------------
def running_work(inp):
    """Background tasks that will wake the session (teammates included), plus scheduled crons."""
    tasks = [t for t in inp.get("background_tasks") or [] if isinstance(t, dict)]
    run = [t for t in tasks if str(t.get("status", "running")).lower() not in TERMINAL]
    if not any(str(t.get("type", "")).lower() == "teammate" for t in tasks) and inp.get("transcript_path"):
        run += [{"type": "teammate", "status": "active (from transcript)", "description": n}
                for n in active_teammates(inp["transcript_path"])]
    crons = [c for c in inp.get("session_crons") or [] if c]
    return run, crons


def active_teammates(path, max_bytes=8_000_000):
    """Teammates still working, from the transcript: spawned or messaged by the lead and not idle or gone since.
    Fallback for when background_tasks carries no teammate entries."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            data = fh.read().decode("utf-8", errors="replace")
    except (OSError, TypeError):
        return []
    spawn_calls, team = {}, {}
    for line in data.splitlines():
        if "teammate" not in line and "Spawned successfully" not in line and '"SendMessage"' not in line \
                and '"TaskStop"' not in line and '"Agent"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        c = (rec.get("message") or {}).get("content")
        for b in c if isinstance(c, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                ti = b.get("input") or {}
                if b.get("name") == "Agent" and ti.get("name"):
                    spawn_calls[b.get("id")] = ti["name"]
                elif b.get("name") == "SendMessage" and (ti.get("to") or ti.get("recipient")) in team:
                    team[ti.get("to") or ti.get("recipient")] = "active"
                elif b.get("name") == "TaskStop":
                    team.pop(ti.get("task_id"), None)
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in spawn_calls:
                if "Spawned successfully" in json.dumps(b.get("content"))[:400]:
                    team[spawn_calls[b["tool_use_id"]]] = "active"
        for part in line.split("<teammate-message ")[1:]:
            m = TM_RE.match("<teammate-message " + part)
            if not m or m.group(1) not in team:
                continue
            body = part[:3000]
            if "teammate_terminated" in body or "shutdown_approved" in body:
                team.pop(m.group(1), None)
            elif "idle_notification" in body:
                team[m.group(1)] = "idle"
    return [n for n, st in team.items() if st == "active"]


def session_tasks_files(transcript, max_bytes=8_000_000):
    """TASKS.md files this session read or wrote (file tools, or a Bash command naming one on its first line),
    oldest first."""
    try:
        size = os.path.getsize(transcript)
        with open(transcript, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            data = fh.read().decode("utf-8", errors="replace")
    except (OSError, TypeError):
        return []
    out = []
    for line in data.splitlines():
        if "tasks.md" not in line.lower():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        c = (rec.get("message") or {}).get("content")
        for b in c if isinstance(c, list) else []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            ti = b.get("input") or {}
            found = []
            if b.get("name") in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
                fp = ti.get("file_path") or ""
                if os.path.basename(fp).lower() == "tasks.md":
                    found.append(fp)
            elif b.get("name") == "Bash":
                first = (ti.get("command") or "").splitlines()[:1]
                found += re.findall(r"([^\s'\"]*(?i:tasks)\.md)\b", first[0]) if first else []
            for fp in found:
                fp = os.path.expanduser(fp)
                if not os.path.isabs(fp):
                    fp = os.path.join(rec.get("cwd") or "", fp)
                if fp not in out:
                    out.append(fp)
    return out


def _ident(p):
    try:
        st = os.stat(p)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def find_tasks(cwd, transcript=None):
    """-> (TASKS.md this session keeps, or None; a TASKS.md found in cwd/project root that the session never
    touched, or None). Found the way the checkpoint code finds it: files the session used, cwd, then the git root.
    A TASKS.md the session never read or wrote belongs to some other run (an orchestrator sharing the directory),
    so it doesn't count."""
    from . import checkpoint
    touched = session_tasks_files(transcript) if transcript else []
    cands = checkpoint.find_tasks_files({"tasks_files": {p: 1 for p in touched}}, cwd)
    owned = {_ident(p) for p in touched} - {None}
    for p in cands:
        if _ident(p) in owned:
            return Path(p), None
    return None, (cands[0] if cands else None)


def task_items(path):
    """-> (open items, ticked count). Items under a BLOCKED heading are not counted as open."""
    if not path:
        return [], 0
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return [], 0
    opened, done, blocked = [], 0, False
    for ln in lines:
        if ln.lstrip().startswith("#"):
            blocked = "blocked" in ln.lower()
            continue
        m = OPEN_RE.match(ln)
        if m and not blocked:
            opened.append(m.group(1)[:200])
        elif DONE_RE.match(ln):
            done += 1
    return opened, done


def _last_text(path):
    """Fallback final message: the last assistant text in the transcript."""
    last = ""
    for rec in _records(path, 0)[0]:
        if rec.get("type") != "assistant":
            continue
        for b in (rec.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                last = b["text"]
    return last


def _records(path, offset):
    """Transcript records from byte `offset`. Returns (records, end offset)."""
    out = []
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except (OSError, TypeError):
        return out, offset
    end = offset + len(data)
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out, end


def _human(text):
    t = text.lstrip()
    return bool(t) and MARK not in t and not t.startswith((
        "<local-command", "<command-", "<system-reminder", "Caveat:", "<task-notification", "[Request interrupted",
        "Stop hook feedback", "Another Claude session sent", "<teammate-message"))


def activity(path, offset, cwd):
    """Since `offset`: file/state-changing tool calls, and whether the owner typed a new prompt."""
    recs, end = _records(path, offset)
    changes, human = 0, False
    cfg = None
    for rec in recs:
        m = rec.get("message") or {}
        c = m.get("content")
        if rec.get("type") == "user" and not rec.get("isMeta"):
            texts = [c] if isinstance(c, str) else [b.get("text", "") for b in c or []
                                                   if isinstance(b, dict) and b.get("type") == "text"]
            human = human or any(_human(t) for t in texts)
        if rec.get("type") != "assistant" or not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name, ti = b.get("name", ""), b.get("input") or {}
            if name in WRITE_TOOLS:
                changes += 1
            elif name.startswith("mcp__") and MCP_WRITE.search(name.split("__")[-1]):
                changes += 1
            elif name == "Bash":
                try:
                    from gutcheck_core import commands
                    cfg = cfg or config.load()
                    if not commands.analyze(ti.get("command", ""), rec.get("cwd") or cwd, cfg).readonly:
                        changes += 1
                except Exception:
                    changes += 1
    return {"changes": changes, "human": human, "end": end}


# ---- classification ------------------------------------------------------------------------------
def build_state(message, open_items, crons=(), task=None):
    msg = message or ""
    if len(msg) > 6000:  # the ending is what decides the stop
        msg = msg[:1500] + "\n[...]\n" + msg[-4400:]
    st = {"final_message": msg,
          "open_items": open_items[:30] if open_items else "none recorded (no task list)",
          "scheduled_wakeups": [str(c)[:200] for c in crons] or "nothing scheduled"}
    if task:
        st["user_request"] = task[:800]
    return st


def classify(message, open_items=(), crons=(), task=None, deadline=20.0):
    """-> {probs, choice, label, early_p, early}. Raises jev.JevError."""
    from gutcheck_core import jev
    q = {"kind": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": KINDS}}
    ans = jev.call(build_state(message, list(open_items), crons, task), q, timeout=deadline, deadline=deadline)["kind"]
    probs = {k: round(float(v), 3) for k, v in ans["probabilities"].items()}
    return decide(probs)


def decide(probs, th=None):
    th = {**DEFAULT_TH, **(th or config.load()["stop"].get("thresholds") or {})}
    early_p = round(sum(probs.get(k, 0) for k in EARLY), 3)
    if early_p >= th["early"]:
        label = max(EARLY, key=lambda k: probs.get(k, 0))
    else:
        label = max((k for k in KINDS if k not in EARLY), key=lambda k: probs.get(k, 0))
    return {"probs": probs, "label": label, "early_p": early_p, "early": label in EARLY}


def last_request(path, max_bytes=2_000_000):
    """The owner's last typed prompt (tail of the transcript), for `user_request`."""
    try:
        size = os.path.getsize(path)
    except (OSError, TypeError):
        return None
    last = None
    for rec in _records(path, max(0, size - max_bytes))[0]:
        if rec.get("type") != "user" or rec.get("isMeta"):
            continue
        c = (rec.get("message") or {}).get("content")
        texts = [c] if isinstance(c, str) else [b.get("text", "") for b in c or []
                                               if isinstance(b, dict) and b.get("type") == "text"]
        for t in texts:
            if _human(t):
                last = t
    return last


# ---- session state, notification, STALLED.md -----------------------------------------------------
def _state_path(session):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session or "unknown")[:120]
    return config.STATE_DIR / "stop_sessions" / f"{safe}.json"


def load_state(session):
    try:
        return json.loads(_state_path(session).read_text())
    except (OSError, ValueError):
        return {}


def save_state(session, st):
    p = _state_path(session)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(p)
    except OSError:
        pass


def log(rec):
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}
        with open(config.STATE_DIR / "stops.jsonl", "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def notify(line, cfg=None):
    """macOS notification (notify-send elsewhere), plus the optional configured push command."""
    line = " ".join(str(line).split())[:200]
    cfg = cfg or config.load()
    try:
        if sys.platform == "darwin" and shutil.which("osascript"):
            script = f'display notification {json.dumps(line, ensure_ascii=False)} with title "Claude long run"'
            subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "Claude long run", line], timeout=5, capture_output=True)
    except Exception:
        pass
    cmd = cfg["stop"].get("notify_cmd")
    if cmd:
        try:
            argv = cmd if isinstance(cmd, list) else [cmd]
            subprocess.run([*argv, line], timeout=10, capture_output=True)
        except Exception:
            pass


def write_stalled(where, open_items, message, history):
    p = Path(where) / "STALLED.md"
    body = ["# STALLED", "", f"Written {time.strftime('%Y-%m-%d %H:%M:%S')} by tokenmaxxing's long-run stop check after "
            "repeated stops with no progress.", "", "## Open items", ""]
    body += [f"- [ ] {it}" for it in open_items] or ["(no TASKS.md open items)"]
    body += ["", "## Last message", "", (message or "").strip()[:6000], "", "## Stop classifications", ""]
    for h in history[-12:]:
        probs = ", ".join(f"{k} {v:.2f}" for k, v in (h.get("probs") or {}).items())
        body.append(f"- {h.get('t')} {h.get('label')} -> {h.get('action')} ({probs})")
    try:
        p.write_text("\n".join(body) + "\n")
        return str(p)
    except OSError:
        return None


def nudge_text(label, open_items, escalate=False):
    if escalate:
        item = open_items[0] if open_items else "the item you are stuck on"
        return (f"{MARK} Several nudges in a row have produced no progress. Write what's blocking \"{item}\" under a "
                "BLOCKED heading in TASKS.md, then continue with any open item that doesn't depend on it.")
    if open_items:
        shown = "; ".join(open_items[:6]) + (f"; and {len(open_items) - 6} more" if len(open_items) > 6 else "")
        return (f"{MARK} Your task list still has open items: {shown}. Continue with them. "
                "If one is blocked, say what is blocking it.")
    return f"{MARK} {NUDGE.get(label, NUDGE['milestone'])} If something is blocked, say what is blocking it."


# ---- the hook ------------------------------------------------------------------------------------
def on_stop(inp):
    if os.environ.get("CLAUDE_LONG_RUN") != "1":
        return None
    t0 = time.monotonic()
    cfg = config.load()
    sc = cfg["stop"]
    mode = sc.get("mode", "log")
    act = mode == "on"
    session = inp.get("session_id") or "unknown"
    path = inp.get("transcript_path") or ""
    cwd = inp.get("cwd") or os.getcwd()
    rec = {"session": session, "mode": mode, "cwd": cwd, "stop_hook_active": bool(inp.get("stop_hook_active"))}

    def finish(action, out=None, **extra):
        rec.update(action=action if act or not out else "would-" + action, ms=int((time.monotonic() - t0) * 1000),
                   **extra)
        log(rec)
        return out if act else None

    run, crons = running_work(inp)
    if run:
        return finish("allow-running", running=[{"type": t.get("type"), "status": t.get("status"),
                                                 "description": str(t.get("description") or "")[:80]}
                                                for t in run[:8]])
    message = inp.get("last_assistant_message") or (_last_text(path) if path else "")
    if not message.strip():
        return finish("allow-no-message")

    tasks, unowned = find_tasks(cwd, path)
    open_items, ticked = task_items(tasks)
    via = "tasks" if tasks else "jev"      # the guide's primary method; Jev only without a task list
    act = mode == "on" and (via == "tasks" or sc.get("jev_fallback", "log") == "on")
    st = load_state(session) if mode == "on" else {}
    size = os.path.getsize(path) if path and os.path.exists(path) else 0
    # progress since the last nudge
    prog = {"ticked": 0, "changes": 0, "human": False}
    if st.get("offset") is not None and path:
        a = activity(path, min(st["offset"], size), cwd)
        prog = {"ticked": max(0, ticked - st.get("ticked", 0)), "changes": a["changes"], "human": a["human"],
                "items_changed": st.get("items_hash") != _hash(open_items)}
        if a["human"]:  # the owner spoke: a new episode
            st = {}
    progressed = bool(prog["ticked"] or prog["changes"] or prog.get("items_changed"))
    rec.update(via=via, open_items=len(open_items), ticked=ticked, tasks=str(tasks) if tasks else None,
               tasks_unowned=unowned, progress=prog, transcript=path, offset=size)

    if via == "tasks":
        if not open_items:
            if act:
                save_state(session, {})
            return finish("allow-tasks-done")
        c = {"label": "open_items", "probs": {}, "early_p": None, "early": True}
    else:
        try:
            c = classify(message, open_items, crons, task=last_request(path) if path else None,
                         deadline=float(sc.get("deadline_s", 20.0)))
        except Exception as e:  # Jev down or rejected: fail open
            return finish("allow-jev-error", error=str(e)[:300])
    rec.update(label=c["label"], early_p=c["early_p"], probs=c["probs"])
    hist = st.get("history", [])
    hist.append({"t": time.strftime("%H:%M:%S"), "label": c["label"], "probs": c["probs"]})

    if not c["early"]:
        action = {"blocker": "allow-legit", "question": "allow-legit", "done": "allow-done",
                  "waiting_on_work": "allow-waiting"}.get(c["label"], "allow-unclear")
        if act:
            save_state(session, {})
            if action == "allow-legit":
                notify(f"Needs you ({c['label']}): {_last_line(message)}", cfg)
        return finish(action)

    # early stop: count stalls without progress
    stalls = 0 if (progressed or not st) else st.get("stalls", 0) + 1
    cap = int(sc.get("max_stalls", 3))
    escalated = st.get("escalated", False) and not progressed
    common = {"ticked": ticked, "items_hash": _hash(open_items), "offset": size}
    if stalls < cap:
        reason = nudge_text(c["label"], open_items)
        hist[-1]["action"] = "block"
        if act:
            save_state(session, {**common, "stalls": stalls, "history": hist[-20:]})
        return finish("block", {"decision": "block", "reason": reason}, stalls=stalls, reason=reason[:500])
    if not escalated:
        reason = nudge_text(c["label"], open_items, escalate=True)
        hist[-1]["action"] = "block-escalate"
        if act:
            save_state(session, {**common, "stalls": stalls, "escalated": True, "history": hist[-20:]})
        return finish("block-escalate", {"decision": "block", "reason": reason}, stalls=stalls, reason=reason[:500])
    hist[-1]["action"] = "allow-stalled"
    stalled = None
    if act:
        stalled = write_stalled(tasks.parent if tasks else cwd, open_items, message, hist)
        save_state(session, {})
        item = open_items[0] if open_items else "the task"
        notify(f"Stalled after {stalls} nudges without progress on {item}. See STALLED.md.", cfg)
    return finish("allow-stalled", stalls=stalls, stalled_md=stalled)


def _hash(items):
    return hashlib.sha1("\n".join(items).encode()).hexdigest()[:12]


def _last_line(text):
    for ln in reversed((text or "").strip().splitlines()):
        if ln.strip():
            return ln.strip()[:160]
    return ""


# ---- promotion: logging-only -> on, judged from what the owner did after each would-block -----------
NOT_OWNER = ("another claude session", "<teammate", "<bash-input", "<bash-stdout", "<bash-stderr",
             "background agent", "<task-notification", "stop hook feedback", "<local-command", "<command-",
             "<system-reminder", "caveat:", "[request interrupted")
GOAHEAD = re.compile(r"^\s*(please\s+)?((ok(ay)?|k|yes|yeah|yup|sure|alright|go)[\s,.!]*)?"
                     r"(so\s+)?(continue|keep going|go on|carry on|keep working|proceed|resume|go( ahead| for it)?|"
                     r"do it|don'?t stop|finish( it| up)?|what'?s next|whats next|what next|next|status|"
                     r"what'?s pending|whats pending|where are we( at)?|still\??)?[\s.!?,]*(please|pls)?[\s.!?]*$", re.I)
PROMOTE = {"min_days": 3, "min_judged": 20, "precision": 0.9}


def _epoch(ts):
    try:
        return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - (time.timezone if ts.endswith("Z") else 0)
    except (TypeError, ValueError):
        return None


def judge(rec):
    """Was a logged would-block right? From the owner's next typed message after that stop:
    a go-ahead or "continue", or a short resume after 20+ minutes of silence -> True (blocking was right);
    a new instruction, an answer or a correction -> False; no owner message yet -> None."""
    recs, _ = _records(rec.get("transcript") or "", int(rec.get("offset") or 0))
    last_ts = None
    for r in recs:
        if r.get("type") == "assistant" and r.get("timestamp"):
            last_ts = r["timestamp"]
        if r.get("type") != "user" or r.get("isMeta"):
            continue
        c = (r.get("message") or {}).get("content")
        texts = [c] if isinstance(c, str) else [b.get("text", "") for b in c or []
                                               if isinstance(b, dict) and b.get("type") == "text"]
        for t in texts:
            t = t.strip()
            if not t or MARK in t or t.lower().startswith(NOT_OWNER):
                continue
            if GOAHEAD.match(t) and len(t) < 60:
                return True, t[:60], r.get("timestamp")
            gap = (_epoch(r.get("timestamp")) or 0) - (_epoch(last_ts) or 1e12)
            if gap >= 1200 and len(t) <= 30 and "?" not in t:
                return True, t[:60], r.get("timestamp")
            return False, t[:60], r.get("timestamp")
    return None, None, None


def maybe_promote(user):
    """Called by the daily refresh. Switches [stop] mode from "log" to "on" once the TASKS.md rule has been
    logging for min_days with at least min_judged judged would-blocks at >= 90% precision. Never loosens.
    Set [stop] auto_promote = false to keep it logging."""
    from . import report
    s = user.setdefault("stop", {})
    if s.get("mode", config.DEFAULTS["stop"]["mode"]) != "log" or s.get("auto_promote") is False:
        return None
    wb = [d for d in report._read("stops") if d.get("via") == "tasks" and d.get("action") == "would-block"]
    if not wb:
        return None
    first = min(time.mktime(time.strptime(d["t"], "%Y-%m-%dT%H:%M:%S")) for d in wb)
    if time.time() - first < PROMOTE["min_days"] * 86400:
        return None
    seen, right, judged = set(), 0, 0
    for d in wb:
        ok, _, ts = judge(d)
        if ok is None or (d.get("session"), ts) in seen:   # consecutive stops before one reply count once
            continue
        seen.add((d.get("session"), ts))
        judged += 1
        right += ok
    if judged < PROMOTE["min_judged"] or right / judged < PROMOTE["precision"]:
        return None
    s["mode"] = "on"
    line = (f"stop check on: {right}/{judged} logged TASKS.md blocks were right "
            f"({right / judged:.0%}) over {(time.time() - first) / 86400:.0f} days")
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.STATE_DIR / "promotions.log", "a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    (config.STATE_DIR / "stop_promoted.notice").write_text(
        f"tokenmaxxing: long-run stop check switched on ({right}/{judged} logged blocks were right); "
        "set [stop] mode = \"log\" in config.toml to undo")
    return line


def pop_notice():
    """The one-line SessionStart notice after a promotion, shown once."""
    p = config.STATE_DIR / "stop_promoted.notice"
    try:
        line = p.read_text().strip()
        p.unlink()
        return line or None
    except OSError:
        return None
