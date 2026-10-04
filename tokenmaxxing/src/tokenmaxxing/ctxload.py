"""Conditional doc loading and memory by relevance (Phase 6, items 2-3).

A tree index per project: project -> file (a project doc or a memory topic file) -> section (markdown heading),
plus a full-text index of the sections. Per prompt, a full-text prefilter (prompt and the assistant's last message)
ranks the sections, and one round of Jev requests sent in parallel asks a Choice over the project's files (its own
request, over the request text only, as before) and a Noul per candidate section ("does this passage hold something
needed for this request?"), split over a few requests. A section loads when its Noul passes and its file is among
the Choice's top picks: the same rule as the earlier two-step version (file Choice first, then the chosen files'
sections), in one round trip instead of two. A Noul request that fails (a firewall block on some passage) or is
late only loses its own sections.

Logging-only by default (`[context] mode = "log"`); `mode = "on"` adds the sections as additionalContext
(short ones inline, long ones as a path + line range). A section is never loaded twice in one session.
Claude Code already loads CLAUDE.md and MEMORY.md itself, so they are not indexed.
"""
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from gutcheck_core import config
from . import skills

DEFAULTS = {
    "mode": "log",                 # log | on | off
    "doc_globs": ["*.md", "docs/*.md", "docs/*/*.md"],
    "exclude": ["CLAUDE.md", "CLAUDE.local.md", "LICENSE.md", "CHANGELOG.md"],
    "memory": True,
    "memory_archive": False,
    "min_file_prob": 0.1, "max_files": 3,
    "min_section": 0.6, "max_sections": 3,
    "inline_chars": 1500, "max_chars": 4000,
    "min_prompt_chars": 12, "deadline_s": 1.8,
    "max_candidates": 80,          # sections sent to Jev per prompt (full-text ranked when a project has more)
    "passage_chars": 3000,         # chars of each section Jev reads
    "batch_chars": 48000,          # passage chars per Jev request; the requests run in parallel
    "projects": {},                # {"<project root>": {"doc_globs": [...]}} in the personal config
}
MAX_FLAT = 100                     # most files in the file Choice
SMALL_FILE = 2500                  # a file this small is one section
MAX_FILE_BYTES = 400_000
MAX_DOC_FILES = 80
SCHEMA_VERSION = 2                 # 2: full-text index of sections (sec_fts)

SCHEMA = """
create table if not exists nodes(id integer primary key, parent integer, level text, project text, kind text,
  label text, grp text, path text, line integer, end_line integer, chars integer);
create index if not exists nodes_parent on nodes(parent);
create index if not exists nodes_project on nodes(project, level);
create table if not exists sources(path text primary key, mtime real, root integer, project text);
create table if not exists projects(root text primary key, indexed real);
create virtual table if not exists sec_fts using fts5(head, body, project unindexed, tokenize='porter unicode61');
"""


def settings():
    return {**DEFAULTS, **(config.load().get("context") or {})}


def connect(write=True):
    db = sqlite3.connect(skills.data_dir() / "context.db", timeout=5)
    if db.execute("pragma user_version").fetchone()[0] < SCHEMA_VERSION:
        # an older index has no full-text rows and would never re-read unchanged files: start it over
        db.executescript("drop table if exists nodes; drop table if exists sources; drop table if exists projects;"
                         "drop table if exists sec_fts;")
        db.executescript(SCHEMA)
        db.execute(f"pragma user_version = {SCHEMA_VERSION}")
        db.commit()
    db.executescript(SCHEMA)
    return db


def connect_for(root):
    """Read side: the first tokenmaxxing data dir whose index has this project (a plugin loaded another way has
    its own data dir); else the writable one."""
    others = [] if config.DATA_DIR_OVERRIDE else \
        sorted((skills.CLAUDE_DIR / "plugins" / "data").glob(f"{skills.SELF_PLUGIN}-*"))
    dirs = [skills.data_dir()] + others + [config.STATE_DIR]
    for d in dirs:
        f = Path(d) / "context.db"
        if not f.exists():
            continue
        try:
            db = sqlite3.connect(f"file:{f}?mode=ro", uri=True, timeout=5)
            if db.execute("pragma user_version").fetchone()[0] >= SCHEMA_VERSION and \
                    db.execute("select 1 from projects where root=?", (root,)).fetchone():
                return db
            db.close()
        except sqlite3.Error:
            pass
    return connect()


def project_root(cwd):
    p = Path(cwd or ".").resolve()
    for d in [p, *p.parents]:
        if (d / ".git").exists():
            return str(d)
        if d == Path.home():
            break
    return str(p)


def memory_dir(root):
    return skills.CLAUDE_DIR / "projects" / re.sub(r"[^A-Za-z0-9]", "-", root) / "memory"


# ---- markdown ---------------------------------------------------------------------------------------------
def headings(lines):
    """[(line index, level, title)] outside code fences."""
    out, fence = [], False
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        m = None if fence else re.match(r"^(#{1,4})\s+(.+?)\s*#*\s*$", ln)
        if m:
            out.append((i, len(m.group(1)), m.group(2)))
    return out


def body_start(lines):
    """Index of the first line after YAML frontmatter."""
    if lines and lines[0].strip() == "---":
        for i in range(1, min(len(lines), 60)):
            if lines[i].strip() == "---":
                return i + 1
    return 0


def sections(text):
    """[(start line 1-based, end line, heading, text)]. Small files are one section."""
    lines = text.splitlines()
    b = body_start(lines)
    if len(text) <= SMALL_FILE:
        hs = headings(lines[b:])
        return [(b + 1, len(lines), hs[0][2] if hs else "", "\n".join(lines[b:]))]
    hs = [(i + b, lv, t) for i, lv, t in headings(lines[b:])]
    top = min((lv for _, lv, _ in hs), default=0)
    cut_lv = top + 1 if sum(lv == top for _, lv, _ in hs) <= 1 else top   # a lone title splits one level down
    cuts = [(i, t) for i, lv, t in hs if lv <= cut_lv]
    out = []
    starts = [(b, "")] + cuts if not cuts or cuts[0][0] > b else cuts
    for k, (s, title) in enumerate(starts):
        e = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        body = "\n".join(lines[s:e])
        if len(body.strip()) > (80 if title else 200):  # a bare title line or a stub is not a section
            out.append((s + 1, e, title or "(top)", body))
    return out or [(b + 1, len(lines), "", "\n".join(lines[b:]))]


def _memory_descriptions(mdir):
    """{file name: description} from MEMORY.md link lines, plus {file name: group heading}."""
    desc, grp = {}, {}
    try:
        text = (mdir / "MEMORY.md").read_text(errors="replace")
    except OSError:
        return desc, grp
    cur = "memory"
    for ln in text.splitlines():
        h = re.match(r"^#{1,3}\s+(.+)", ln)
        if h:
            cur = h.group(1).strip()
        for m in re.finditer(r"\[([^\]]+)\]\(([^)\s]+\.md)\)\s*[—:-]*\s*(.*)", ln):
            name = Path(m.group(2)).name
            desc[name] = (m.group(1) + (" — " + m.group(3) if m.group(3) else "")).strip()
            grp[name] = cur
    return desc, grp


# ---- indexing ---------------------------------------------------------------------------------------------
def _files(root, st):
    rootp = Path(root)
    pst = (st.get("projects") or {}).get(root) or {}
    globs = pst.get("doc_globs") or st["doc_globs"]
    excl = set(st["exclude"])
    docs = []
    for g in globs:
        for p in sorted(rootp.glob(g)):
            if p.is_file() and p.name not in excl and not p.name.startswith(".") and p not in docs:
                docs.append(p)
    out = [("doc", p, None, None) for p in docs[:MAX_DOC_FILES]]
    if st.get("memory", True):
        mdir = memory_dir(root)
        desc, grp = _memory_descriptions(mdir)
        pats = ["*.md"] + (["archive/*.md"] if st.get("memory_archive") else [])
        for pat in pats:
            for p in sorted(mdir.glob(pat)):
                if p.name != "MEMORY.md":
                    arch = "/archive/" in str(p)
                    out.append(("memory", p, desc.get(p.name), "memory (archived)" if arch else
                                "memory: " + grp.get(p.name, "other")))
    return out


def _add_file(db, root, kind, path, desc, grp):
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    secs = sections(text)
    rel = os.path.relpath(path, root) if kind == "doc" else f"memory/{path.name}"
    if kind == "memory":
        from .skills import frontmatter
        desc = desc or frontmatter(text).get("description") or (secs[0][2] if secs else path.stem)
        label = f"memory {path.stem}: {desc}"
    else:
        heads = [t for _, _, t, _ in secs if t][:10]
        first = next((ln.strip() for ln in text.splitlines()[body_start(text.splitlines()):]
                      if ln.strip() and not ln.startswith("#")), "")
        label = f"doc {rel}: {first[:140]}" + (f" | sections: {' / '.join(heads)}" if heads else "")
        grp = grp or f"docs: {os.path.dirname(rel) or 'project root'}"
    cur = db.execute("insert into nodes(parent,level,project,kind,label,grp,path,line,end_line,chars) "
                     "values(null,'file',?,?,?,?,?,1,?,?)",
                     (root, kind, label[:400], grp, str(path), text.count("\n") + 1, len(text)))
    fid = cur.lastrowid
    for s, e, title, body in secs:
        snippet = re.sub(r"\s+", " ", body)[:160]
        sid = db.execute("insert into nodes(parent,level,project,kind,label,grp,path,line,end_line,chars) "
                         "values(?,'section',?,?,?,?,?,?,?,?)",
                         (fid, root, kind, f"{title}: {snippet}"[:300], grp, str(path), s, e, len(body))).lastrowid
        db.execute("delete from sec_fts where rowid=?", (sid,))  # a row an older version left behind
        db.execute("insert into sec_fts(rowid, head, body, project) values(?,?,?,?)",
                   (sid, f"{label[:400]} {title}", body, root))
    db.execute("insert or replace into sources(path,mtime,root,project) values(?,?,?,?)",
               (str(path), path.stat().st_mtime, fid, root))


def _drop(db, path):
    row = db.execute("select root from sources where path=?", (path,)).fetchone()
    if row:
        db.execute("delete from sec_fts where rowid in (select id from nodes where parent=?)", (row[0],))
        db.execute("delete from nodes where id=? or parent=?", (row[0], row[0]))
        db.execute("delete from sources where path=?", (path,))


def index_project(root, db=None):
    """(Re)index one project's docs and memory; only changed files are re-read."""
    st = settings()
    own = db is None
    db = db or connect()
    want = {str(p): (k, p, d, g) for k, p, d, g in _files(root, st)}
    have = {p: m for p, m in db.execute("select path, mtime from sources where project=?", (root,))}
    for p in set(have) - set(want):
        _drop(db, p)
    n = 0
    for p, (kind, path, desc, grp) in want.items():
        try:
            m = path.stat().st_mtime
        except OSError:
            continue
        if have.get(p) is None or have[p] < m:
            _drop(db, p)
            _add_file(db, root, kind, path, desc, grp)
            n += 1
    db.execute("insert or replace into projects(root, indexed) values(?,?)", (root, time.time()))
    db.commit()
    if own:
        db.close()
    return n


def recent_projects(days=30):
    """Project roots of sessions from the last `days` days (from the transcripts' cwd)."""
    from gutcheck_core import transcripts
    roots = set()
    for f in transcripts.files(since=time.time() - days * 86400, include_subagents=False):
        try:
            with open(f, errors="replace") as fh:
                for _, line in zip(range(40), fh):
                    if '"cwd"' in line:
                        cwd = json.loads(line).get("cwd")
                        if cwd and os.path.isdir(cwd):
                            roots.add(project_root(cwd))
                        break
        except (OSError, ValueError):
            pass
    skip = ("/private/", "/tmp/", str(config.STATE_DIR), str(skills.data_dir()))
    return sorted(r for r in roots if not r.startswith(skip) and "/scratchpad" not in r)


def build(quiet=True, days=30):
    db = connect()
    total, projects = 0, recent_projects(days)
    for root in projects:
        total += index_project(root, db)
    db.close()
    if not quiet:
        print(f"context: {len(projects)} projects, {total} files (re)indexed")
    return total


# ---- routing ------------------------------------------------------------------------------------------------
PICK_FILE = ("Which of these project notes or docs, if any, holds rules, facts, decisions or procedures that the "
             "assistant needs in order to handle the user's latest request correctly?")
NONE_DESC = "None of them: the request can be handled from the conversation, the code and general knowledge."
NEED = ("Consider only passages[\"{k}\"]. Does this passage hold a rule, fact, decision or procedure that the "
        "assistant needs to handle the user's latest request correctly (not merely the same topic)?")
STOP = set("""the and but for with that this these those then than from into onto about what why how when where which
who whom there here also just not yes does did done have has had can could would should will its it's are was were
been being you your yours our ours they them their she her him his let lets like dont cant wont isnt thats okay
some any all more most other only own same such both each few again once out over under very too now yeah sure
please thanks make need want know think still even ever much many well really actually""".split())


def terms(text, limit=40):
    """Distinct content words of `text` (hyphenated and snake_case names split), for the full-text query."""
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text or ""):
        for t in re.split(r"[-_]", w.lower()):
            if len(t) > 2 and t not in STOP and t not in out:
                out.append(t)
    return out[:limit]


def _fts(db, root, words):
    if not words:
        return []
    expr = " OR ".join(f'"{t}"' for t in words)
    try:
        return [r[0] for r in db.execute("select rowid from sec_fts where sec_fts match ? and project=? "
                                         "order by bm25(sec_fts, 3.0, 1.0) limit 400", (expr, root))]
    except sqlite3.Error:
        return []


def candidates(db, root, prompt, context, k):
    """Up to k section ids, best first: reciprocal-rank fusion of a full-text match on the prompt and a weaker
    one on the assistant's last message; a project with no more than k sections sends them all."""
    ids = [r[0] for r in db.execute("select id from nodes where project=? and level='section' order by id", (root,))]
    if len(ids) <= k:
        return ids
    score = {}
    for weight, words in ((1.0, terms(prompt)), (0.6, terms(context[-1200:]))):
        for rank, i in enumerate(_fts(db, root, words)):
            score[i] = score.get(i, 0.0) + weight / (10 + rank)
    ranked = sorted(score, key=lambda i: -score[i])[:k]
    seen = set(ranked)
    return ranked + [i for i in ids if i not in seen][:k - len(ranked)]


def route(root, prompt, context="", deadline=None, seen=()):
    """Returns {"sections": [...], "files": [...], ...}. Sections carry path, lines, heading, text, p."""
    from concurrent.futures import ThreadPoolExecutor, wait
    from gutcheck_core import jev
    st = settings()
    t0 = time.monotonic()
    rec = {"project": root}

    def left():
        return None if deadline is None else max(0.1, deadline - (time.monotonic() - t0))

    db = connect_for(root)
    try:
        files = db.execute("select id, label, grp, path from nodes where project=? and level='file'",
                           (root,)).fetchall()
        if not files:
            known = db.execute("select 1 from projects where root=?", (root,)).fetchone()
            rec["status"] = "empty" if known else "not indexed"
            return rec
        for path, m in db.execute("select path, mtime from sources where project=?", (root,)):
            try:
                if os.path.getmtime(path) > m:
                    rec["stale"] = True  # the caller re-indexes in the background
                    break
            except OSError:
                rec["stale"] = True
                break
        cand = candidates(db, root, prompt, context, st["max_candidates"])
        secs = []
        for i in cand:
            row = db.execute("select n.id, n.parent, n.label, n.path, n.line, n.end_line, f.body from nodes n "
                             "join sec_fts f on f.rowid = n.id where n.id=?", (i,)).fetchone()
            if row:
                secs.append(row)
    finally:
        db.close()
    rec["candidates"] = len(secs)
    # the file Choice: every file, or (a very large project) the files that own candidates first
    if len(files) > MAX_FLAT:
        own = {s[1] for s in secs}
        files = ([f for f in files if f[0] in own] + [f for f in files if f[0] not in own])[:MAX_FLAT]
    state = {"request": prompt[:3000]}
    if context:
        state["recent_context"] = context[-1200:]
    crit = {f"f{i}": f[1] for i, f in enumerate(files)}
    crit["none"] = NONE_DESC
    batches, cur, size = [[]], [], 0   # batch 0: the file Choice alone
    for n, s in enumerate(secs):
        text = s[6][:st["passage_chars"]]
        if cur and size + len(text) > st["batch_chars"]:
            batches.append(cur)
            cur, size = [], 0
        cur.append((n, text))
        size += len(text)
    if cur:
        batches.append(cur)

    def ask(b, items):
        if b == 0:
            return jev.call(state, {"which": {"type": "choice", "instructions": PICK_FILE, "criteria": crit}},
                            deadline=left())
        qs = {f"n{n}": {"type": "noul", "instructions": NEED.format(k=f"s{n}")} for n, _ in items}
        return jev.call(dict(state, passages={f"s{n}": t for n, t in items}), qs, deadline=left())

    ex = ThreadPoolExecutor(len(batches))
    futs = [ex.submit(ask, b, items) for b, items in enumerate(batches)]
    wait(futs, timeout=None if deadline is None else left() + 0.2)
    ex.shutdown(wait=False)
    rec["requests"] = len(batches)
    ans = futs[0].result(timeout=0)  # the file Choice is required; a JevError here fails open upstream
    for f in futs[1:]:
        try:
            ans.update(f.result(timeout=0))
        except Exception:
            rec["partial"] = rec.get("partial", 0) + 1  # sections of a late or failed request are skipped
    probs = ans["which"]["probabilities"]
    rec["none_file"] = probs.get("none", 0.0)
    ranked = sorted(((p, k) for k, p in probs.items() if k != "none"), reverse=True)
    chosen = {files[int(k[1:])][0]: p for p, k in ranked[:st["max_files"]] if p >= st["min_file_prob"]}
    rec["files"] = [(f[3], round(chosen[f[0]], 3)) for f in files if f[0] in chosen]
    if not chosen:
        rec["status"] = "none"
        rec["ms"] = int((time.monotonic() - t0) * 1000)
        return rec
    scored = sorted(((ans[f"n{n}"]["noul"], n) for n, s in enumerate(secs)
                     if s[1] in chosen and f"n{n}" in ans), reverse=True)
    out = []
    for p, n in scored:
        if p < st["min_section"] or len(out) >= st["max_sections"]:
            break
        s = secs[n]
        key = f"{s[3]}#{s[4]}"
        out.append({"key": key, "path": s[3], "line": s[4], "end": s[5], "heading": s[2].split(":")[0],
                    "p": round(p, 3), "text": s[6], "repeat": key in seen})
    rec["sections"] = out
    rec["status"] = "ok"
    rec["ms"] = int((time.monotonic() - t0) * 1000)
    return rec


def render(root, sections, st=None):
    """additionalContext text for the sections (short ones inline, long ones as a pointer)."""
    st = st or settings()
    parts, used = [], 0
    for s in sections:
        if s["path"].startswith(root + os.sep):
            rel = os.path.relpath(s["path"], root)
        elif "/memory/" in s["path"]:
            rel = "memory/" + s["path"].split("/memory/", 1)[1]
        else:
            rel = s["path"].replace(str(Path.home()), "~")
        where = f"{rel} § {s['heading']}" if s["heading"] and s["heading"] != "(top)" else rel
        if len(s["text"]) <= st["inline_chars"] and used + len(s["text"]) <= st["max_chars"]:
            parts.append(f"tokenmaxxing context ({where}):\n{s['text'].strip()}")
            used += len(s["text"])
        else:
            parts.append(f"tokenmaxxing context: relevant section {s['path']} lines {s['line']}-{s['end']} "
                         f"({where}); read it before acting.")
    return "\n\n".join(parts)
