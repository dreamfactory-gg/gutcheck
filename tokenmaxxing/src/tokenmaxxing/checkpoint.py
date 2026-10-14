"""Durable compaction: a checkpoint written before compaction (PreCompact) and re-injected after it
(SessionStart, source "compact"); transitions.py also writes it at SessionEnd for a /clear or a restart.

It opens with "WHERE WE ARE" and the TASKS.md Now block (transitions.prepare; its backup is the one Claude call),
then is built from the transcript with local code plus Jev only:
  - the owner's standing instructions and the lead's instructions to a teammate, verbatim, from the per-session
    rules store (rules.py), which captures them as they are stated and is caught up here;
  - TASKS.md open items and their done bars, plus the session's open todo list;
  - key decisions (the owner's answers to questions; decisions stated in the agent's text);
  - running background work: background Bash, monitors, async subagents and teammates, with IDs;
  - files touched, branches, documents in play (as pointers, never their content).

The standing rules have their own budget and are never truncated. Everything else stays under CAP_TOKENS: open
items and running work first, then the rest, which turns into pointers (the uncapped version, recall).
"""
import hashlib
import json
import os
import re
import subprocess
import time
from collections import OrderedDict
from pathlib import Path

from gutcheck_core import config
from gutcheck_core.messages import (
    _HEUR,
    _SENT_RE,
    _clean,
    _fmt_ts,
    _texts,
    est_tokens,
    owner_source,
)

CAP_TOKENS = 4800            # the spec's ceiling is ~5k; keep a margin for the estimate
JEV_DEADLINE = 25.0
MAX_JEV_ITEMS = 1200           # per checkpoint; older unanswered units fall back to the keyword heuristic
DECISION_MIN_P = 0.6

DECISION_CONDITION = (
    "Does this text state a decision that has been made in the session (a chosen approach, design, scope, "
    "or a ruling) that later work should follow, rather than a status report, a plan still open, or a question?")

ENDED = ("completed", "failed", "killed", "stopped", "error", "cancelled", "canceled", "timeout", "expired")
_NOTIF_RE = re.compile(r"<task-id>([^<]+)</task-id>.*?<status>(\w+)</status>", re.S)
_BG_RE = re.compile(r"running in background with ID: (\w+)")
_MON_RE = re.compile(r"Monitor started \(task (\w+)")
_TEAM_RE = re.compile(r"agent_id: (\S+)")
_ASYNC_RE = re.compile(r"agentId: (\w+)")
_OUT_RE = re.compile(r"Output is being written to: (\S+?)\.?(?:\s|$)")
_BRANCH_RE = re.compile(r"git\s+(?:-C\s+\S+\s+)?(?:checkout\s+-[bB]|switch\s+-[cC]|branch)\s+([\w./-]+)|"
                        r"git\s+(?:-C\s+\S+\s+)?worktree\s+add\s+(?:-b\s+([\w./-]+)\s+)?(\S+)")
_DECISION_CUE = re.compile(r"(?i)\b(decided|decision|chose|chosen|going with|we'll use|settled on|ruling|verdict|"
                           r"approved|agreed|switched to|picked|opted|the plan is|owner's call)\b")
_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def checkpoint_dir():
    d = config.plugin_data()
    return (Path(d) if d else config.STATE_DIR) / "checkpoints"


def paths_for(session):
    d = checkpoint_dir()
    safe = re.sub(r"[^\w.-]", "_", str(session or "unknown"))
    return d / f"{safe}.md", d / f"{safe}.full.md", d / f"{safe}.json"


def session_name(s):
    t = s["titles"]
    return t.get("custom-title") or t.get("agent-name") or t.get("ai-title")


# ---- transcript scan -------------------------------------------------------------------------------
def _result_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _strings(obj):
    """Every string in a JSON record (task notifications arrive in several record shapes)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def scan(path, until_line=None):
    """Read a transcript (up to `until_line`, exclusive) into the facts a checkpoint needs."""
    s = {"owner": [], "asks": [], "agent_text": [], "bg": OrderedDict(), "files": OrderedDict(), "branches": [],
         "docs": OrderedDict(), "tasks_files": OrderedDict(), "todos": None, "cwd": None, "git_branch": None,
         "session": None, "last_ts": None, "lead_heard": {}, "titles": {}}
    uses, pend_stop = {}, {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return s
    with fh:
        for n, line in enumerate(fh):
            if until_line is not None and n >= until_line:
                break
            if "task-notification" in line:
                try:
                    d0 = json.loads(line)
                except ValueError:
                    d0 = None
                for st in _strings(d0) if d0 else ():
                    if "<task-notification>" in st:
                        for tid, status in _NOTIF_RE.findall(st):
                            if status.lower() in ENDED:
                                _end(s, tid.strip(), status.lower())
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("isSidechain"):
                continue
            t = d.get("type")
            if t == "system" and d.get("subtype") == "agents_killed":
                for k in [k for k, v in s["bg"].items() if v["kind"] in ("teammate", "agent")]:
                    _end(s, k, "killed")
            if t == "attachment":
                a = d.get("attachment") or {}
                # the owner typing while the agent works: delivered as a queued command, not a user message
                if a.get("type") == "queued_command" and a.get("commandMode", "prompt") == "prompt":
                    for txt in _texts(a.get("prompt")):
                        src = owner_source(txt)
                        if src:
                            s["owner"].append({"ts": d.get("timestamp"), "src": src, "text": _clean(txt)})
                continue
            if t in ("custom-title", "agent-name", "ai-title"):
                s["titles"][t] = d.get("customTitle") or d.get("agentName") or d.get("aiTitle")
            if t not in ("user", "assistant"):
                continue
            ts = d.get("timestamp")
            s["last_ts"] = ts or s["last_ts"]
            s["cwd"] = d.get("cwd") or s["cwd"]
            s["session"] = d.get("sessionId") or s["session"]
            if d.get("gitBranch") and d.get("gitBranch") != "HEAD":
                s["git_branch"] = d["gitBranch"]
            m = d.get("message") or {}
            c = m.get("content") if isinstance(m, dict) else None
            if t == "user":
                if d.get("isMeta") or d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly"):
                    continue
                for txt in _texts(c):
                    src = owner_source(txt)
                    if src:
                        s["owner"].append({"ts": ts, "src": src, "text": _clean(txt)})
                    tm = re.match(r'\s*(?:Another Claude session sent a message:\s*)?<teammate-message teammate_id="([^"]+)"', txt)
                    if tm:
                        s["lead_heard"][tm.group(1)] = ts
                for b in c if isinstance(c, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        _on_result(s, uses, pend_stop, b, ts)
            else:
                for b in c if isinstance(c, list) else []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text"):
                        s["agent_text"].append({"ts": ts, "text": b["text"]})
                    elif b.get("type") == "tool_use":
                        _on_use(s, uses, pend_stop, b, ts)
    return s


def _end(s, tid, status):
    v = s["bg"].pop(tid, None)
    if v is None:  # teammates are keyed by "name@team"; notifications may use the bare name
        for k in list(s["bg"]):
            if k.split("@")[0] == tid:
                s["bg"].pop(k)


def _on_use(s, uses, pend_stop, b, ts):
    name, inp = b.get("name") or "", b.get("input") or {}
    uses[b.get("id")] = {"name": name, "input": inp, "ts": ts}
    if name in _EDIT_TOOLS:
        p = inp.get("file_path") or inp.get("notebook_path")
        if p:
            s["files"].pop(p, None)
            s["files"][p] = ts
            if os.path.basename(p).lower() == "tasks.md":
                s["tasks_files"][p] = ts
    elif name == "Read":
        p = inp.get("file_path") or ""
        if p.endswith(".md"):
            s["docs"].pop(p, None)
            s["docs"][p] = ts
            if os.path.basename(p).lower() == "tasks.md":
                s["tasks_files"][p] = ts
    elif name == "Bash":
        cmd = inp.get("command", "")
        for m in _BRANCH_RE.finditer(cmd):
            br = m.group(1) or m.group(2) or m.group(3)
            if br and not br.startswith("-") and br not in s["branches"]:
                s["branches"].append(br)
        if "TASKS.md" in cmd:
            for p in re.findall(r"(/[\w./-]*TASKS\.md)", cmd):
                s["tasks_files"][p] = ts
    elif name in ("TaskStop", "KillShell", "KillBash"):
        tid = inp.get("task_id") or inp.get("shell_id") or inp.get("id")
        if tid:
            pend_stop[b.get("id")] = tid
    elif name == "CronDelete":
        tid = inp.get("id") or inp.get("cron_id") or inp.get("job_id")
        if tid:
            pend_stop[b.get("id")] = tid
    elif name == "TodoWrite":
        s["todos"] = inp.get("todos")


def _on_result(s, uses, pend_stop, b, ts):
    uid = b.get("tool_use_id")
    txt = _result_text(b.get("content"))
    if uid in pend_stop:
        tid = pend_stop.pop(uid)
        if not b.get("is_error"):
            _end(s, tid, "stopped")
        return
    u = uses.get(uid)
    if not u or b.get("is_error"):
        return
    name, inp = u["name"], u["input"]
    if name == "Bash" and inp.get("run_in_background"):
        m = _BG_RE.search(txt)
        if m:
            out = _OUT_RE.search(txt)
            s["bg"][m.group(1)] = {"kind": "bash", "desc": inp.get("description") or inp.get("command", "")[:120],
                                   "started": u["ts"], "output": out.group(1) if out else None}
    elif name == "Monitor":
        m = _MON_RE.search(txt)
        if m:
            s["bg"][m.group(1)] = {"kind": "monitor", "desc": inp.get("description") or "", "started": u["ts"],
                                   "timeout_ms": inp.get("timeout_ms")}
    elif name in ("Agent", "Task"):
        m = _TEAM_RE.search(txt)
        if m and "Spawned successfully" in txt:
            s["bg"][m.group(1)] = {"kind": "teammate", "desc": inp.get("description") or "", "started": u["ts"],
                                   "name": inp.get("name"), "type": inp.get("subagent_type")}
            return
        m = _ASYNC_RE.search(txt)
        if m and ("launched" in txt.lower() or "running in the background" in txt.lower()
                  or "async" in txt.lower()):
            s["bg"][m.group(1)] = {"kind": "agent", "desc": inp.get("description") or "", "started": u["ts"],
                                   "name": inp.get("name"), "type": inp.get("subagent_type")}
    elif name == "CronCreate":
        m = re.search(r"\b(?:id|ID)[:\s]+([\w-]{4,})", txt)
        if m:
            s["bg"][m.group(1)] = {"kind": "cron", "desc": (inp.get("prompt") or inp.get("description") or "")[:120],
                                   "started": u["ts"], "schedule": inp.get("cron") or inp.get("schedule")}
    elif name == "ListAgents":
        listed = set(re.findall(r"^\s+([\w.-]+) \[[0-9a-f]{4,}\]", txt, re.M))
        if listed:
            for k in [k for k, v in s["bg"].items() if v["kind"] == "teammate"]:
                if k.split("@")[0] not in listed and (s["bg"][k].get("name") or "") not in listed:
                    s["bg"].pop(k)
    elif name == "AskUserQuestion":
        if "answered" in txt and "No response" not in txt:
            s["asks"].append({"ts": ts, "text": txt})


def _ts_epoch(ts):
    try:
        return time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except Exception:
        return None


def running(s, now_ts=None):
    """Background work still running at the end of the scan (monitors past their timeout are dropped)."""
    now = _ts_epoch(now_ts or s["last_ts"] or "") or time.time()
    out = []
    for k, v in s["bg"].items():
        if v["kind"] == "monitor":
            st = _ts_epoch(v.get("started") or "")
            lim = (v.get("timeout_ms") or 300_000) / 1000
            if st and now - st > lim + 60:
                continue
        out.append((k, v))
    return out


# ---- owner units and Jev ------------------------------------------------------------------------------
def _cache_path(session):
    return checkpoint_dir() / "cache" / f"{re.sub(r'[^\w.-]', '_', str(session or 'unknown'))}.json"


def _h(kind, text):
    return hashlib.sha1((kind + "\0" + text).encode("utf-8", "replace")).hexdigest()[:16]


def classify(items, condition, kind, session, deadline=JEV_DEADLINE):
    """items: list of text. Returns ({text: p}, how) with Jev answers cached per session.
    how: 'jev', 'jev+heuristic' (some answers missing) or 'heuristic' (Jev unavailable)."""
    from gutcheck_core import jev
    cp = _cache_path(session)
    try:
        cache = json.loads(cp.read_text())
    except Exception:
        cache = {}
    out, todo = {}, []
    for t in items:
        k = _h(kind, t)
        if k in cache:
            out[t] = cache[k]
        else:
            todo.append(t)
    how = "jev"
    if todo:
        try:
            if not jev.have_key():
                raise jev.JevError("no key")
            keyed = [(str(i), t) for i, t in enumerate(todo)][-MAX_JEV_ITEMS:]   # the newest, if there are very many
            probs = jev.noul_each(keyed, condition, deadline=deadline)
            for i, t in enumerate(todo):
                p = probs.get(str(i))
                if p is not None:
                    out[t] = float(p)
                    cache[_h(kind, t)] = float(p)
        except Exception:
            pass
        missing = [t for t in todo if t not in out]
        if missing:
            how = "heuristic" if len(missing) == len(todo) and not (set(items) - set(todo)) else "jev+heuristic"
            for t in missing:
                out[t] = 0.55 if (kind == "rule" and _HEUR.search(t)) or \
                    (kind == "decision" and _DECISION_CUE.search(t)) else 0.0
        _cache_merge(cp, {_h(kind, t): out[t] for t in todo if t in out and t not in missing})
    return out, how


def _cache_merge(path, new):
    """Merge answers into the per-session cache under a lock (re-read, merge, atomic rename), so parallel
    classifiers (prompt capture, Stop, PreCompact) never drop each other's entries."""
    from gutcheck_core.lock import locked
    if not new:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a") as lk, locked(lk):
            try:
                cache = json.loads(path.read_text())
            except Exception:
                cache = {}
            cache.update(new)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(cache))
            tmp.replace(path)
    except Exception:
        pass


# ---- TASKS.md ------------------------------------------------------------------------------------------
def git_root(cwd):
    try:
        return subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True,
                              text=True, timeout=3).stdout.strip() or None
    except Exception:
        return None


def find_tasks_files(s, cwd):
    cands = list(reversed(s["tasks_files"].keys()))
    roots = [r for r in (cwd, cwd and git_root(cwd)) if r]
    for r in roots:
        for name in ("TASKS.md", "tasks.md"):
            cands.append(os.path.join(r, name))
    seen, out = set(), []
    for p in cands:
        try:
            st = os.stat(p)   # inode, not path: TASKS.md and tasks.md are one file on a case-insensitive disk
        except OSError:
            continue
        rp = (st.st_dev, st.st_ino)
        if rp in seen or not os.path.isfile(p):
            continue
        seen.add(rp)
        out.append(p)
    return out[:3]


def open_items(path):
    """[(section, item_text, done_bar_or_None)] for unchecked items. A done bar is an inline 'Done bar:'/'Done when'
    clause or an indented line under the item that starts with one."""
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    out, section = [], ""
    bar_re = re.compile(r"(?i)\b(done bar|done when|done if|definition of done|acceptance)\b\s*[:\-]?\s*")
    for i, line in enumerate(lines):
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            continue
        m = re.match(r"\s*[-*]\s+\[ \]\s+(.*)", line)
        if not m:
            continue
        text, bar = m.group(1).strip(), None
        bm = bar_re.search(text)
        if bm:
            bar = text[bm.start():].strip()
            text = text[:bm.start()].strip().rstrip(".;")
        for nxt in lines[i + 1:i + 4]:
            if re.match(r"\s*[-*]\s+\[[ xX]\]", nxt) or nxt.startswith("#") or not nxt.strip():
                break
            if bar_re.search(nxt) and nxt.startswith((" ", "\t")):
                bar = nxt.strip().lstrip("-* ")
                break
        out.append((section, text, bar))
    return out


# ---- build ---------------------------------------------------------------------------------------------
def _short(t, n):
    t = " ".join(t.split())
    return t if len(t) <= n else t[:n - 1] + "…"


def build(transcript_path, session=None, cwd=None, trigger=None, custom_instructions=None, until_line=None,
          now_ts=None, deadline=JEV_DEADLINE, cap_tokens=CAP_TOKENS, s=None, now=None, source=None):
    """Returns (capped_markdown, full_markdown, meta). `now`: the Now block, put first; `source`: what the
    checkpoint is for (default: compaction)."""
    t0 = time.monotonic()
    s = s or scan(transcript_path, until_line=until_line)
    session = session or s["session"]
    cwd = cwd or s["cwd"]

    # standing rules: captured as stated (rules.py); catch up from the transcript and classify what is pending
    from gutcheck_core import rules as rules_mod
    try:
        sync_st = rules_mod.sync(session, transcript_path, until_line=until_line, deadline=deadline)
    except Exception as e:
        sync_st = {"error": f"{type(e).__name__}: {e}"}
    rules_md, rules_meta = rules_mod.section(session)
    # decision candidates: recent agent sentences with a decision cue, and the owner's answers to questions
    dec_cands = []
    for at in s["agent_text"][-400:]:
        for para in at["text"].split("\n"):
            for sent in _SENT_RE.split(para.strip()):
                if 25 <= len(sent) <= 400 and _DECISION_CUE.search(sent):
                    dec_cands.append((at["ts"], sent.strip()))
    dec_cands = dec_cands[-80:]

    dec_p, dec_how = classify(list(dict.fromkeys(t for _, t in dec_cands)), DECISION_CONDITION, "decision",
                              session, deadline)
    decisions = [(ts, t) for ts, t in dec_cands if dec_p.get(t, 0.0) >= DECISION_MIN_P]
    seen_d, dd = set(), []
    for ts, t in reversed(decisions):
        if t not in seen_d:
            seen_d.add(t)
            dd.append((ts, t))
    decisions = list(reversed(dd[:10]))
    asks = s["asks"][-6:]

    tasks_files = find_tasks_files(s, cwd)
    tasks = [(p, open_items(p)) for p in tasks_files]
    todos = [t for t in (s["todos"] or []) if isinstance(t, dict) and t.get("status") != "completed"]
    run = running(s, now_ts)
    files = list(reversed(s["files"].keys()))
    docs = [p for p in reversed(s["docs"].keys()) if p not in s["files"]][:8]
    branch = s["git_branch"]

    full_path = paths_for(session)[1]

    # --- sections (each: title, lines) ---
    kind = {"manual": "manual ", "auto": "automatic "}.get(trigger, "")
    head = [f"# WHERE WE ARE (from {source or kind + 'compaction'}, {_fmt_ts(now_ts or s['last_ts'])} UTC)"]
    if custom_instructions:
        head.append(f"Owner's note for this compaction: {custom_instructions.strip()}")
    if now:
        head.append("\n" + now.strip())
    foot = ["", "Written from the transcript by tokenmaxxing. The standing instructions are the owner's own words and still "
            "apply. For anything not here, run `tokenmaxxing recall \"<question>\"` (past sessions, memory, git) or "
            f"read the full checkpoint: {full_path}"]

    open_lines = []
    for p, items in tasks:
        if not items:
            open_lines.append(f"- {p}: no open items")
            continue
        open_lines.append(f"From {p} ({len(items)} open):")
        for sec, text, bar in items:
            open_lines.append(f"- [ ] {text}" + (f" ({sec})" if sec else "") + (f" — {bar}" if bar else ""))
    for t in todos:
        open_lines.append(f"- [ ] (todo) {t.get('content') or t.get('subject') or ''} [{t.get('status')}]")

    run_lines = []
    for k, v in run:
        extra = ""
        if v["kind"] == "bash" and v.get("output"):
            extra = f"; output {v['output']}"
        if v["kind"] == "teammate" and v.get("name") in s["lead_heard"]:
            extra = f"; last message {_fmt_ts(s['lead_heard'][v['name']])}"
        if v["kind"] == "cron" and v.get("schedule"):
            extra = f"; schedule {v['schedule']}"
        kind = "teammate (persistent; idle or working)" if v["kind"] == "teammate" else v["kind"]
        run_lines.append(f"- {kind} `{k}`: {_short(v.get('desc') or '', 110)} (started "
                         f"{_fmt_ts(v.get('started'))}{extra})")

    dec_lines = [f"- [{_fmt_ts(ts)}] {_short(t, 240)}" for ts, t in decisions]
    dec_lines += [f"- [{_fmt_ts(a['ts'])} owner answered] {_short(a['text'].replace('Your questions have been answered:', '').replace('The user answered:', ''), 300)}"
                  for a in asks]

    ctx_lines = []
    if cwd:
        ctx_lines.append(f"- cwd: {cwd}" + (f"; branch {branch}" if branch else ""))
    if s["branches"]:
        ctx_lines.append(f"- branches created or switched to: {', '.join(s['branches'][-8:])}")
    if files:
        ctx_lines.append(f"- files edited ({len(files)}, most recent first): " + ", ".join(files[:25])
                         + (f", and {len(files) - 25} more" if len(files) > 25 else ""))
    if docs:
        ctx_lines.append("- documents read (re-read the section you need, not the whole file): " + ", ".join(docs))

    rules_block = rules_md.strip() or "## Standing instructions from the owner\n- none recorded"

    def render(parts, with_rules=True):
        out = list(head)
        for i, (title, lines) in enumerate(parts):
            if lines:
                out.append(f"\n## {title}")
                out.extend(lines)
            if i == 0 and with_rules:  # after the open items
                out.append("\n" + rules_block)
        return "\n".join(out + foot) + "\n"

    full_parts = [("Open items", open_lines), ("Running background work", run_lines),
                  ("Key decisions", dec_lines), ("Files and branches", ctx_lines)]
    full = render(full_parts)

    # --- capped version. The standing rules sit outside the cap (their own budget, never truncated); the cap
    # applies to everything else: open items and running work stay (terse), decisions and files become pointers.
    budget = cap_tokens
    room = budget - est_tokens(now or "")  # the Now block sits in the head, which the later renders count

    def fit(lines, cap, pointer):
        if est_tokens("\n".join(lines)) <= cap:
            return list(lines)
        out = [x if len(x) <= 180 else x[:179] + "…" for x in lines]
        while out and est_tokens("\n".join(out + [pointer])) > cap:
            out.pop()
        return out + [pointer]

    ol = fit(open_lines, int(room * 0.45),
             f"- (list cut to fit; the rest is in {', '.join(p for p, _ in tasks) or full_path})")
    rn = fit(run_lines, int(room * 0.25), f"- (more running work in the full checkpoint: {full_path})")
    parts = [("Open items", ol), ("Running background work", rn)]
    for title, lines in (("Key decisions", dec_lines), ("Files and branches", ctx_lines)):
        if not lines:
            continue
        pointer = f"- (more in the full checkpoint: {full_path})"
        if est_tokens(render(parts + [(title, lines)], False)) <= budget:
            parts.append((title, lines))
            continue
        keep = list(lines)
        while keep and est_tokens(render(parts + [(title, keep + [pointer])], False)) > budget:
            keep.pop()
        if est_tokens(render(parts + [(title, keep + [pointer])], False)) <= budget:
            parts.append((title, keep + [pointer]))
    capped = render(parts)
    rest_tokens = est_tokens(render(parts, False))

    meta = {"session": session, "name": session_name(s),
            "now": bool(now), "rules": rules_meta.get("owner_rules", 0) + rules_meta.get("lead_rules", 0),
            **rules_meta, "rules_sync": sync_st, "rule_how": "jev" if not rules_meta.get("pending") else "partial",
            "decision_how": dec_how, "decisions": len(decisions), "open_items":
                sum(len(i) for _, i in tasks) + len(todos), "tasks_files": tasks_files, "running": len(run_lines),
            "files": len(files), "tokens": est_tokens(capped), "rest_tokens": rest_tokens,
            "full_tokens": est_tokens(full), "ms": int((time.monotonic() - t0) * 1000)}
    return capped, full, meta


def write(inp, deadline=JEV_DEADLINE, source=None, s=None):
    """PreCompact and SessionEnd: build and write the checkpoint, Now block first (transitions.py). Returns meta."""
    from gutcheck_core import secretscan

    from . import transitions
    session, tp = inp.get("session_id"), inp.get("transcript_path") or ""
    s = s or scan(tp)
    capped, full, meta = build(tp, session=session, cwd=inp.get("cwd"), trigger=inp.get("trigger"),
                               custom_instructions=inp.get("custom_instructions"),
                               now_ts=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), deadline=deadline, s=s,
                               now=transitions.prepare(inp, s), source=source)
    md, fullp, metap = paths_for(session)
    md.parent.mkdir(parents=True, exist_ok=True)
    for p, text in ((md, capped), (fullp, full)):
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(secretscan.redact(text))
        tmp.replace(p)
    meta.update(path=str(md), transcript=inp.get("transcript_path"), written=time.time())
    metap.write_text(json.dumps(meta))
    return meta


def for_session_start(inp, max_age_s=6 * 3600):
    """SessionStart with source 'compact': the checkpoint text to re-inject, or None."""
    if inp.get("source") != "compact":
        return None
    md, _, metap = paths_for(inp.get("session_id"))
    if not md.exists():
        # a session id that changed across compaction: fall back to the newest checkpoint of the same transcript
        tp = inp.get("transcript_path")
        best = None
        for mp in checkpoint_dir().glob("*.json"):
            try:
                m = json.loads(mp.read_text())
            except Exception:
                continue
            if tp and m.get("transcript") == tp and (best is None or m.get("written", 0) > best[0]):
                best = (m.get("written", 0), Path(m["path"]))
        if not best:
            return None
        md = best[1]
    try:
        if time.time() - md.stat().st_mtime > max_age_s:
            return None
        text = md.read_text()
    except OSError:
        return None
    return text.rstrip() + "\n\nUse `tokenmaxxing recall \"<question>\"` for anything else from before the compaction."


def main(args):
    """tokenmaxxing checkpoint build TRANSCRIPT [--until-line N] [--full] [--meta]"""
    import sys
    if not args or args[0] != "build" or len(args) < 2:
        sys.exit(main.__doc__)
    until = None
    if "--until-line" in args:
        until = int(args[args.index("--until-line") + 1])
    from gutcheck_core import secretscan
    capped, full, meta = build(args[1], until_line=until)
    print(secretscan.redact(full if "--full" in args else capped))
    if "--meta" in args:
        print(json.dumps(meta), file=sys.stderr)
