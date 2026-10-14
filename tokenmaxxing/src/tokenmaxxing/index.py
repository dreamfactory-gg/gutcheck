"""Tree index of past work: project -> session -> task -> chunk, with a short label at each level.

Sources: Claude Code transcripts, memory files (including memory/archive), Codex rollouts, git history.
Chunks keep redacted excerpts (transcripts are deleted after `cleanupPeriodDays`, the index outlives them).
Search (recall.py) narrows with full-text search, then opens branches level by level with Jev.
"""
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from gutcheck_core import config, secretscan, transcripts

DB = config.STATE_DIR / "index.db"
CODEX_DIR = Path(os.environ.get("GUTCHECK_CODEX_DIR", Path.home() / ".codex"))
# Where recall looks for git repos: GUTCHECK_GIT_ROOT (one dir), else `[recall] git_roots` in the config, else the
# common code folders under your home that exist.
GIT_ROOT = os.environ.get("GUTCHECK_GIT_ROOT")
DEFAULT_GIT_ROOTS = ["~/projects", "~/code", "~/src", "~/dev", "~/repos", "~/work", "~/git"]


def git_roots():
    if GIT_ROOT:
        return [Path(GIT_ROOT)]
    roots = config.load().get("recall", {}).get("git_roots") or DEFAULT_GIT_ROOTS
    return [Path(os.path.expanduser(r)) for r in roots]
CHUNK_CHARS = 1500
HOME = str(Path.home())

SCHEMA = """
create table if not exists nodes(id integer primary key, parent integer, level text, label text, source text,
  path text, line integer, ts text, kind text);
create index if not exists nodes_parent on nodes(parent);
create table if not exists sources(path text primary key, mtime real, root integer);
create virtual table if not exists chunks using fts5(text, label, node unindexed, tokenize='porter unicode61');
create virtual table if not exists errs using fts5(err, cmd unindexed, after unindexed, path unindexed, line unindexed,
  ts unindexed, project unindexed, tokenize='porter unicode61');
create virtual table if not exists launches using fts5(brief, kind unindexed, path unindexed, line unindexed,
  ts unindexed, project unindexed, result unindexed, tokenize='porter unicode61');
"""
VERSION = 8  # bump to re-index everything when the schema or extraction changes


def connect(readonly=False):
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    if readonly:
        if not DB.exists():
            return None
        db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        return db
    db = sqlite3.connect(DB, timeout=30)
    if db.execute("pragma user_version").fetchone()[0] < VERSION:
        db.close()  # built by an older extractor: start over (the next build re-reads every source)
        for suffix in ("", "-wal", "-shm"):
            Path(str(DB) + suffix).unlink(missing_ok=True)
        db = sqlite3.connect(DB, timeout=30)
        db.execute(f"pragma user_version={VERSION}")
    db.execute("pragma journal_mode=WAL")  # hooks read while a refresh writes
    db.executescript(SCHEMA)
    return db


def _node(db, parent, level, label, source="", path="", line=0, ts="", kind=""):
    # labels cut from prompts reach Jev through recall; project names come from the cwd and are looked up as stored
    label = label if level == "project" else secretscan.redact(label)
    cur = db.execute("insert into nodes(parent,level,label,source,path,line,ts,kind) values(?,?,?,?,?,?,?,?)",
                     (parent, level, label[:300], source, path, line, ts, kind))
    return cur.lastrowid


_where = {}  # task node id -> "project | session label" (matched by full-text search along with the text)


def _chunk(db, parent, text, label, path, line, ts, kind):
    text = secretscan.redact(text)
    nid = _node(db, parent, "chunk", label[:200], "", path, line, ts, kind)
    if parent not in _where:
        row = db.execute("select s.label, p.label from nodes t join nodes s on s.id=t.parent "
                         "join nodes p on p.id=s.parent where t.id=?", (parent,)).fetchone()
        _where[parent] = f"{row[1]} | {row[0]}" if row else ""
    db.execute("insert into chunks(rowid,text,label,node) values(?,?,?,?)",
               (nid, text, secretscan.redact(_where[parent])[:400], nid))


_projects = {}


def _project(db, name):
    if name in _projects:
        return _projects[name]
    row = db.execute("select id from nodes where level='project' and label=?", (name,)).fetchone()
    pid = row[0] if row else _node(db, None, "project", name)
    _projects[name] = pid
    return pid


def project_name(cwd):
    if not cwd:
        return "unknown"
    cwd = cwd.replace(HOME, "~")
    m = re.match(r"^(~/projects/[^/]+(?:/[^/]+)?)", cwd)
    if m:
        return m.group(1)
    if "/scratchpad" in cwd or cwd.startswith(("/private/tmp", "/tmp")):
        return "scratch"
    return cwd


def _drop(db, path):
    row = db.execute("select root from sources where path=?", (path,)).fetchone()
    if not row:
        return
    db.execute("delete from errs where path=?", (path,))
    db.execute("delete from launches where path=?", (path,))
    if row[0] is None:
        db.execute("delete from sources where path=?", (path,))
        return
    ids, frontier = [], [row[0]]
    while frontier:
        ids += frontier
        q = ",".join("?" * len(frontier))
        frontier = [r[0] for r in db.execute(f"select id from nodes where parent in ({q})", frontier)]
    q = ",".join("?" * len(ids))
    db.execute(f"delete from chunks where rowid in ({q})", ids)
    db.execute(f"delete from nodes where id in ({q})", ids)
    db.execute("delete from sources where path=?", (path,))


def _stale(db, path):
    path = str(path)
    try:
        m = os.path.getmtime(path)
    except OSError:
        return False
    row = db.execute("select mtime from sources where path=?", (path,)).fetchone()
    return not row or row[0] < m


# ---- Claude transcripts ------------------------------------------------------------------------
SKIP_ERR = transcripts.REJECT_MARKERS + (transcripts.CLASSIFIER_MARKER, transcripts.INTERRUPT_MARKER,
                                         "Cancelled: parallel tool call", "<tool_use_error>Sibling")
EVAL_DIRS = (str(config.STATE_DIR / "evals"), str(config.STATE_DIR).replace(HOME, "~") + "/evals")


def _mark(db, path, root):
    db.execute("insert or replace into sources(path,mtime,root) values(?,?,?)", (path, os.path.getmtime(path), root))


def _after(evs, i, names, limit=900):
    """What happened right after event i, until the owner's next prompt: narration, commands and outcomes."""
    out, size = [], 0
    for e in evs[i + 1:i + 14]:
        if transcripts.is_human_prompt(e):
            break
        if e["kind"] == "text" and e["text"].strip():
            piece = "CLAUDE: " + e["text"].strip()[:400]
        elif e["kind"] == "tool":
            inp = e["input"]
            if e["name"] == "Bash":
                piece = f"$ {inp.get('command', '')[:200]}"
            elif e["name"] in ("Edit", "Write", "MultiEdit"):
                piece = f"[{e['name']} {inp.get('file_path', '')}]"
            else:
                continue
        elif e["kind"] == "result" and names.get(e["id"]) == "Bash":
            piece = "-> error" if e.get("error") else "-> ok"
        else:
            continue
        out.append(piece)
        size += len(piece)
        if size > limit:
            break
    return "\n".join(out)[:limit]


def index_transcript(db, path):
    path = str(path)
    evs = list(transcripts.events(path))
    title, cwd, ts0 = None, None, None
    with open(path, errors="replace") as fh:
        for line in fh:
            if '"ai-title"' in line:
                try:
                    title = json.loads(line).get("aiTitle") or title
                except ValueError:
                    pass
            if cwd is None and '"cwd"' in line:
                try:
                    d = json.loads(line)
                    cwd, ts0 = d.get("cwd"), d.get("timestamp")
                except ValueError:
                    pass
    prompts = [e for e in evs if transcripts.is_human_prompt(e)]
    _drop(db, path)
    if not prompts or (cwd and cwd.startswith(EVAL_DIRS)):
        _mark(db, path, None)  # nothing to index (or tokenmaxxing's own eval runs); don't re-read until it changes
        return
    first_prompt = prompts[0]["text"].strip().replace("\n", " ")
    sub = "/subagents/" in path or Path(path).parent.parent != transcripts.PROJECTS
    date = (ts0 or "")[:10]
    proj = project_name(cwd)
    label = f"{date} {'(subagent) ' if sub else ''}{title or first_prompt[:140]}"
    sid = _node(db, _project(db, proj), "session", label, "claude", path, 0, ts0 or "", "claude")
    _mark(db, path, sid)
    task, buf, buf_ts, n = None, [], None, 0
    names, cmds = {}, {}

    def flush():
        nonlocal buf
        if task and buf:
            text = "\n".join(buf)
            _chunk(db, task, text, text[:160], path, n, buf_ts or "", "claude")
        buf = []

    for i, e in enumerate(evs):
        if transcripts.is_human_prompt(e):
            flush()
            ptxt = e["text"].strip()
            task = _node(db, sid, "task", ptxt.replace("\n", " ")[:240], "claude", path, i, e.get("ts") or "",
                         "prompt")
            _chunk(db, task, "OWNER: " + ptxt[:3000], ptxt[:160], path, i, e.get("ts") or "", "prompt")
            continue
        if e["kind"] == "tool":
            names[e["id"]] = e["name"]
            if e["name"] == "Bash":
                cmds[e["id"]] = e["input"].get("command", "")
        if e["kind"] == "result" and e.get("error") and names.get(e["id"]) == "Bash" and e["text"].strip() \
                and not any(m in e["text"] for m in SKIP_ERR):
            db.execute("insert into errs(err,cmd,after,path,line,ts,project) values(?,?,?,?,?,?,?)",
                       (secretscan.redact(e["text"][:2000]), secretscan.redact(cmds.get(e["id"], ""))[:600],
                        secretscan.redact(_after(evs, i, names)), path, i, e.get("ts") or "", proj))
        if task is None:
            continue
        if e["kind"] == "text" and e["text"].strip():
            piece = "CLAUDE: " + e["text"].strip()[:2500]
        elif e["kind"] == "tool":
            inp = e["input"]
            if e["name"] == "Bash":
                piece = f"$ {inp.get('description', '')}: {inp.get('command', '')[:300]}"
            elif e["name"] in ("Edit", "Write", "Read"):
                piece = f"[{e['name']} {inp.get('file_path', '')}]"
            elif e["name"] == "Agent":
                piece = f"[Agent {inp.get('subagent_type', '')}: {inp.get('prompt', '')[:400]}]"
            else:
                piece = f"[{e['name']}]"
        elif e["kind"] == "result" and e.get("error"):
            piece = f"ERROR ({names.get(e['id'], 'tool')}): {e['text'][:600]}"
        else:
            continue
        if not buf:
            buf_ts, n = e.get("ts"), i
        buf.append(piece)
        if sum(len(x) for x in buf) > CHUNK_CHARS:
            flush()
    flush()
    if "/subagents/" in path:
        # a subagent run: its brief and its final report, for "don't redo work"
        texts = [e["text"].strip() for e in evs if e["kind"] == "text" and e["text"].strip()]
        db.execute("insert into launches(brief,kind,path,line,ts,project,result) values(?,?,?,?,?,?,?)",
                   (secretscan.redact(prompts[0]["text"][:4000]), "agent", path, 0, ts0 or "", proj,
                    secretscan.redact(texts[-1][:1500]) if texts else ""))


def _dir_cwd(d):
    """The working directory a ~/.claude/projects/<dashed> dir stands for: a transcript's cwd, else the dashed
    name resolved against the filesystem ('-' may stand for '/', '.', '_' or itself)."""
    for f in sorted(d.glob("*.jsonl"))[:5]:
        with open(f, errors="replace") as fh:
            for line in fh:
                if '"cwd"' in line:
                    try:
                        return json.loads(line)["cwd"]
                    except (ValueError, KeyError):
                        break
    parts, cur, i = d.name.lstrip("-").split("-"), "/", 0
    while i < len(parts):
        for j in range(len(parts), i, -1):
            found = None
            for joiner in ("-", ".", "_"):
                seg = joiner.join(parts[i:j])
                seg = "." + seg[1:] if seg.startswith(joiner) and joiner != "." and seg[:1] == "" else seg
                if seg and os.path.exists(os.path.join(cur, seg)):
                    found = seg
                    break
                if parts[i] == "" and os.path.exists(os.path.join(cur, "." + ".".join(parts[i + 1:j]))):
                    found = "." + ".".join(parts[i + 1:j])
                    break
            if found:
                cur, i = os.path.join(cur, found), j
                break
        else:
            return os.path.join(cur, "-".join(parts[i:]))
    return cur


# ---- memory --------------------------------------------------------------------------------------
def index_memory(db, path):
    path = str(path)
    text = Path(path).read_text(errors="replace")
    proj = project_name(_dir_cwd(Path(path).parents[2 if "/archive/" in path else 1]))
    _drop(db, path)
    mid = _node(db, _project(db, proj), "session", f"memory: {Path(path).stem}" +
                (" (archived)" if "/archive/" in path else ""), "memory", path, 0, "", "memory")
    db.execute("insert or replace into sources(path,mtime,root) values(?,?,?)", (path, os.path.getmtime(path), mid))
    tid = _node(db, mid, "task", Path(path).stem, "memory", path, 0, "", "memory")
    for k in range(0, len(text), CHUNK_CHARS):
        part = text[k:k + CHUNK_CHARS]
        _chunk(db, tid, part, part[:160], path, text[:k].count("\n") + 1, "", "memory")


# ---- Codex rollouts ------------------------------------------------------------------------------
_SKIP_USER = re.compile(r"^\s*(<(environment_context|user_instructions|context_window_guidance|permissions|"
                        r"recommended_plugins|codex_internal_context|goal_context|user_shell_command|turn_aborted)|"
                        r"The following is the Codex agent history)")


def index_codex(db, path):
    path = str(path)
    from .recall import key_line
    cwd, ts0, tasks = None, None, []
    cur, errs, open_errs = None, [], []

    def follow(piece):
        for e in list(open_errs):
            e["after"].append(piece)
            if len(e["after"]) >= 10 or sum(len(x) for x in e["after"]) > 900:
                open_errs.remove(e)

    with open(path, errors="replace") as fh:
        for ln, line in enumerate(fh):
            if len(line) > 200_000:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t, p = d.get("type"), d.get("payload") or {}
            if t == "session_meta":
                cwd, ts0 = p.get("cwd"), p.get("timestamp")
            elif t == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                txt = "\n".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict))
                if txt.lstrip().startswith("# AGENTS.md instructions"):
                    continue
                if txt.strip() and not _SKIP_USER.match(txt):
                    cur = {"prompt": txt, "line": ln, "ts": d.get("timestamp"), "result": ""}
                    tasks.append(cur)
                    open_errs.clear()
            elif t == "response_item" and p.get("type") == "message" and p.get("role") == "assistant":
                txt = " ".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict)).strip()
                if txt:
                    follow("CLAUDE: " + txt[:400])  # same label as Claude transcripts, so one parser reads both
            elif t == "event_msg" and p.get("type") == "item_completed" and \
                    (p.get("item") or {}).get("type") == "CommandExecution":
                it = p["item"]
                cmd = it.get("command") or []
                cmd = cmd[-1] if isinstance(cmd, list) and cmd else str(cmd)
                failed = it.get("exit_code") not in (0, None)
                follow(f"$ {cmd[:200]}\n-> {'error' if failed else 'ok'}")
                if failed and cur is not None:
                    out = (it.get("stderr") or "") + "\n" + (it.get("stdout") or "")
                    if key_line(out):
                        e = {"err": f"Exit code {it.get('exit_code')}\n{out.strip()[-2000:]}", "cmd": cmd, "line": ln,
                             "ts": d.get("timestamp"), "after": []}
                        errs.append(e)
                        open_errs.append(e)
            elif t == "event_msg" and p.get("type") == "task_complete" and cur is not None:
                cur["result"] = p.get("last_agent_message") or ""
    _drop(db, path)
    if not tasks:
        _mark(db, path, None)
        return
    first = next((ln.strip() for tk in tasks for ln in tk["prompt"].splitlines()
                  if ln.strip() and not ln.lstrip().startswith(("#", "<")) and "AGENTS.md" not in ln), "codex task")
    sid = _node(db, _project(db, project_name(cwd)), "session",
                f"{(ts0 or '')[:10]} (codex) {first[:140]}", "codex", path, 0,
                ts0 or "", "codex")
    db.execute("insert or replace into sources(path,mtime,root) values(?,?,?)", (path, os.path.getmtime(path), sid))
    for e in errs:
        db.execute("insert into errs(err,cmd,after,path,line,ts,project) values(?,?,?,?,?,?,?)",
                   (secretscan.redact(e["err"]), secretscan.redact(e["cmd"])[:600],
                    secretscan.redact("\n".join(e["after"]))[:900], path, e["line"], e["ts"] or "",
                    project_name(cwd)))
    for tk in tasks:
        tid = _node(db, sid, "task", tk["prompt"].strip().replace("\n", " ")[:240], "codex", path, tk["line"],
                    tk["ts"] or "", "prompt")
        _chunk(db, tid, "BRIEF: " + tk["prompt"][:3000], tk["prompt"][:160], path, tk["line"], tk["ts"] or "", "codex")
        db.execute("insert into launches(brief,kind,path,line,ts,project,result) values(?,?,?,?,?,?,?)",
                   (secretscan.redact(tk["prompt"][:4000]), "codex", path, tk["line"], tk["ts"] or "",
                    project_name(cwd), secretscan.redact(tk["result"][:1500])))
        if tk["result"]:
            _chunk(db, tid, "CODEX RESULT: " + tk["result"][:3000], tk["result"][:160], path, tk["line"],
                   tk["ts"] or "", "codex")


# ---- git -------------------------------------------------------------------------------------------
def index_git(db, repo, since_days=365):
    key = f"git:{repo}"
    head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    row = db.execute("select mtime from sources where path=?", (key,)).fetchone()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        return  # no commits
    stamp = float(int(head[:8], 16))
    if row and row[0] == stamp:
        return
    fmt = "--format=%H%x1f%ad%x1f%s%x1f%b%x1f%ae%x1e"
    try:
        out = subprocess.run(["git", "-C", repo, "log", f"--since={since_days}.days", fmt, "--date=short"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return
    recs = [r.strip().split("\x1f") for r in out.split("\x1e") if r.strip()]
    mine = [r for r in recs if len(r) >= 5 and _owner(r[4])]
    if len(recs) > 300 and len(mine) < 0.1 * len(recs):
        recs = mine  # someone else's project (a clone); keep only the owner's own commits
    _drop(db, key)
    sid = _node(db, _project(db, project_name(repo)), "session", f"git history of {repo.replace(HOME, '~')}",
                "git", repo, 0, "", "git")
    db.execute("insert or replace into sources(path,mtime,root) values(?,?,?)", (key, stamp, sid))
    for parts in recs:
        if len(parts) < 3:
            continue
        sha, date, subj = parts[0], parts[1], parts[2]
        body = parts[3] if len(parts) > 3 else ""
        if re.match(r"Merge (branch|pull request|remote-tracking|commit) ", subj) and not body.strip():
            continue  # default merge messages say nothing; merges with a real message are kept
        tid = _node(db, sid, "task", f"commit {date}: {subj}", "git", repo, 0, date, "git")
        _chunk(db, tid, f"COMMIT {sha[:10]} {date}: {subj}\n{body.strip()[:1200]}", subj, repo, 0, date,
               "git")


_owner_ids = None


def _owner(email):
    """Is this author email one of the owner's (git config identities, plus the Claude co-author address)?"""
    global _owner_ids
    if _owner_ids is None:
        ids = set()
        for key in ("user.email",):
            v = subprocess.run(["git", "config", "--global", key], capture_output=True, text=True).stdout.strip()
            if v:
                ids.add(v.lower())
        ids.add("noreply@anthropic.com")
        _owner_ids = ids
    return email.lower() in _owner_ids


def git_repos(root=None, depth=3):
    if root is None:
        seen, out = set(), []
        for r in git_roots():
            for d in git_repos(r, depth):
                if d not in seen:
                    seen.add(d)
                    out.append(d)
        return out
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for dirpath, dirnames, _ in os.walk(root):
        d = Path(dirpath)
        if len(d.relative_to(root).parts) > depth:
            dirnames[:] = []
            continue
        if (d / ".git").is_dir():
            out.append(str(d))
            dirnames[:] = [x for x in dirnames if x not in ("node_modules", "target", ".git")]
        dirnames[:] = [x for x in dirnames if not x.startswith(".") and x not in ("node_modules", "target", "venv")]
    return out


def build(quiet=True, codex_days=365):
    """Incremental: only sources whose mtime (or git HEAD) changed are re-read. One build at a time."""
    from contextlib import ExitStack

    from gutcheck_core.lock import locked
    t0 = time.time()
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.STATE_DIR / "index.lock", "w") as lock, ExitStack() as stack:
        try:
            stack.enter_context(locked(lock, block=False))
        except OSError:
            if not quiet:
                print("index: another build is running")
            return None
        db = connect()
        n = {"claude": 0, "memory": 0, "codex": 0, "git": 0}
        for p in transcripts.files():
            if _stale(db, p):
                index_transcript(db, p)
                n["claude"] += 1
        for p in list(transcripts.PROJECTS.glob("*/memory/*.md")) + list(transcripts.PROJECTS.glob("*/memory/archive/*.md")):
            if p.name != "MEMORY.md" and "-gutcheck-evals-" not in str(p) and _stale(db, p):
                index_memory(db, p)
                n["memory"] += 1
        cutoff = time.time() - codex_days * 86400
        for p in (CODEX_DIR / "sessions").rglob("rollout-*.jsonl"):
            try:
                if p.stat().st_mtime > cutoff and _stale(db, p):
                    index_codex(db, p)
                    n["codex"] += 1
            except OSError:
                pass
        for repo in git_repos():
            index_git(db, repo)
            n["git"] += 1
        db.commit()
        db.close()
        if not quiet:
            print(f"index: updated {n} in {time.time() - t0:.0f}s -> {DB}")
        return n
