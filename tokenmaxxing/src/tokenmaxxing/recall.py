"""`tokenmaxxing recall "QUESTION"`: what did we decide / how did we fix X / how did we approach X last time.

Search the tree index (index.py): full-text search narrows to candidate chunks; Jev then opens branches
level by level (project -> session -> task) with one Choice per level, and a Noul per surviving chunk keeps
only passages that help answer. Returns pointers and short excerpts, never whole transcripts.
"""
import json
import os
import re
import sys
import time

from gutcheck_core import config, jev, secretscan

from . import index

STOP = set("""a an the and or of to in on for with from by at as is are was were be been it this that these those
what which who whom how why when where did do does done we you i our us they them their there here about into
over under again last time before after then than can could should would will just also any some all not no
make made use used using get got fix fixed couldn didn doesn wasn isn aren won even though had has have out say
said back end one came come same every everything actually thing things way ended kept keep put take took went
while still ever never really very much many more most other others such only like want wanted need needed let
approach approached decide decided happen happened happening cause caused causing change changed stop stopped
behind whole instead up down off onto per via its it's""".split())


def keywords(q):
    words = [w.strip(".-").lower() for w in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*[A-Za-z0-9_]", q)]
    seen, out = set(), []
    for w in words:
        if len(w) >= 3 and w not in STOP and w not in seen:
            seen.add(w)
            out.append(w)
    return out[:16]


_df_cache = {}


def _df(db, term):
    if term not in _df_cache:
        try:
            _df_cache[term] = db.execute("select count(*) from chunks where chunks match ?",
                                         ('"' + term.replace('"', "") + '"',)).fetchone()[0]
        except Exception:
            _df_cache[term] = 0
    return _df_cache[term]


def _terms(db, q):
    """Keywords that narrow: drop terms found in more than ~4% of chunks (keep the 3 rarest regardless).
    Adjacent words are also tried joined ("Open Cloud" -> opencloud, "Digital Ocean" -> digitalocean)."""
    words = keywords(q)
    raw = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", q)]
    joined = [a + b for a, b in zip(raw, raw[1:]) if len(a) >= 2 and len(b) >= 3 and a not in STOP and b not in STOP]
    kws = [k for k in dict.fromkeys(words + joined) if _df(db, k) > 0]
    if not kws:
        return []
    total = db.execute("select count(*) from nodes where level='chunk'").fetchone()[0] or 1
    by_rarity = sorted(kws, key=lambda k: _df(db, k))
    keep = [k for k in by_rarity if _df(db, k) <= 0.08 * total]
    return keep if len(keep) >= 5 else by_rarity[:5]


def _fts(db, q, limit=400):
    kws = _terms(db, q)
    if not kws:
        return []
    expr = " OR ".join('"' + k.replace('"', "") + '"' for k in kws)
    return db.execute("select rowid, bm25(chunks, 1.0, 0.4) as s from chunks where chunks match ? order by s limit ?",
                      (expr, limit)).fetchall()


def _ancestors(db, nid):
    chain = []
    while nid is not None:
        row = db.execute("select id,parent,level,label,path,line,ts,source from nodes where id=?", (nid,)).fetchone()
        if not row:
            break
        chain.append(row)
        nid = row[1]
    return chain  # chunk, task, session, project


def _text(db, nid):
    row = db.execute("select text from chunks where rowid=?", (nid,)).fetchone()
    return row[0] if row else ""


def _windows(text, width=520, step=380):
    if len(text) <= width + 120:
        return [(0, text)]
    out = []
    for k in range(0, len(text), step):
        out.append((k, text[k:k + width]))
        if k + width >= len(text):
            break
    return out


def _density_window(text, kws, width=260):
    """The window with the most distinct keywords (a cheap snippet for labelling a session)."""
    best, best_n = 0, -1
    low = text.lower()
    for k in range(0, max(1, len(text) - width + 1), 60):
        n = sum(1 for w in kws if w in low[k:k + width])
        if n > best_n:
            best, best_n = k, n
    return text[best:best + width].strip()


def _hedged(fn, hedge_after=2.0, tries=2, total=None):
    """Run fn() on a daemon thread; if it hasn't answered after `hedge_after` seconds, start a duplicate and take
    whichever finishes first (Jev's latency is usually well under a second but has multi-second spells).
    Daemon threads, so a slow loser never holds up the process's exit (hooks, CLI)."""
    import queue
    import threading
    q = queue.Queue()

    def go():
        try:
            q.put((True, fn()))
        except Exception as e:  # noqa: BLE001 - handed to the caller below
            q.put((False, e))

    started, errors, t0 = 0, [], time.monotonic()
    while True:
        if started < tries:
            threading.Thread(target=go, daemon=True).start()
            started += 1
        wait = hedge_after if started < tries else None
        if total is not None:
            left = total - (time.monotonic() - t0)
            if left <= 0:
                raise jev.JevError("deadline exceeded")
            wait = left if wait is None else min(wait, left)
        try:
            ok, val = q.get(timeout=wait)
        except queue.Empty:
            continue
        if ok:
            return val
        errors.append(val)
        if len(errors) >= started and started >= tries:
            raise errors[0]
        if isinstance(val, jev.JevError) and val.waf:
            raise val  # a content block: a duplicate would be blocked too


def _noul(items, condition, deadline=None, size=40, context=None):
    """noul_each in parallel batches of `size`, each hedged against a slow response. A batch refused by a
    firewall (some text in it looks like an attack to the gateway) is split in halves to isolate the item;
    items that can't be scored get None."""
    from concurrent.futures import ThreadPoolExecutor

    def run(batch, depth=0):
        try:
            return _hedged(lambda: jev.noul_each(batch, condition, context=context, deadline=deadline), total=deadline)
        except jev.JevError as e:
            if not e.waf or len(batch) == 1 or depth >= 3:
                return {k: None for k, _ in batch}
            h = len(batch) // 2
            return {**run(batch[:h], depth + 1), **run(batch[h:], depth + 1)}

    parts = [items[k:k + size] for k in range(0, len(items), size)]
    out = {}
    with ThreadPoolExecutor(max(1, len(parts))) as ex:
        for r in ex.map(run, parts):
            out.update(r)
    return out


def _candidates(db, question, days=None, exclude=(), before=None, limit=500):
    """FTS hits grouped by task node: ([task ids in best-bm25 order], {task: [chunk ids]}, {node id: row})."""
    hits = _fts(db, question, limit)
    by_task, meta, order, seen_text = {}, {}, [], set()
    cutoff = _days_ago(days) if days else None
    for nid, score in hits:
        chain = _ancestors(db, nid)
        if len(chain) < 4:
            continue
        chunk, task, sess = chain[0], chain[1], chain[2]
        when = (chunk[6] or sess[6] or "")[:19]
        if cutoff and when and when[:10] < cutoff:
            continue
        if before and when and when >= before:
            continue
        if exclude and any(x in (sess[4] or "") for x in exclude):
            continue
        text = _text(db, nid)
        sig = hash(text[:400])
        if sig in seen_text:  # the same commit in several clones, the same memory copied
            continue
        seen_text.add(sig)
        for row in chain:
            meta[row[0]] = row
        if task[0] not in by_task:
            by_task[task[0]] = []
            order.append(task[0])
        by_task[task[0]].append(nid)
    return order, by_task, meta


def _result(meta, nid, p, excerpt):
    chunk = meta[nid]
    task, sess = meta[chunk[1]], meta[meta[chunk[1]][1]]
    return {"node": nid, "p": round(p, 2), "project": meta[sess[1]][3], "session": sess[3], "task": task[3][:160],
            "source": f"{chunk[4]}:{chunk[5]}" if chunk[5] else chunk[4],
            "when": (chunk[6] or sess[6] or "")[:10], "excerpt": excerpt}


def _label(meta, tid):
    task = meta[tid]
    sess = meta[task[1]]
    t = "" if task[3] == sess[3] or sess[7] == "memory" else f" > {task[3][:120]}"
    return f"{meta[sess[1]][3]} | {sess[3][:120]}{t}"


def search(question, max_passages=6, days=None, exclude=(), before=None, mode="jev", tasks=40, keep=10,
           per_task=3, min_p=0.35, deadline=None):
    """Tree search over the index. FTS narrows to candidate tasks (an owner prompt and its work, a Codex brief,
    a memory file, a commit); one Jev call scores each from its place in the tree (project > session > task)
    and its best-matching snippet; a second scores ~500-char windows of the kept tasks' matching chunks.
    Returns pointers (source path:line) with the best window as the excerpt, never whole transcripts."""
    db = index.connect()
    order, by_task, meta = _candidates(db, question, days, exclude, before)
    if not order:
        return []
    kws = _terms(db, question)
    if mode == "fts":
        out = []
        for tid in order[:max_passages]:
            nid = by_task[tid][0]
            out.append(_result(meta, nid, 0, _excerpt(_text(db, nid), question)))
        return out
    # level 1: tasks, labelled by their place in the tree and their best snippets
    cand = order[:tasks]
    items = []
    for tid in cand:
        snips = " … ".join(_density_window(_text(db, n), kws) for n in by_task[tid][:2])
        items.append((str(tid), f"{_label(meta, tid)}\n{snips}"))
    ps = _noul(items, "Is this past work (its place in the history and matching snippets) likely to "
                      f"contain the answer to this question: {question}", deadline=deadline)
    for i, t in enumerate(cand):  # unscored (Jev unreachable or refused): fall back to the full-text order
        if ps.get(str(t)) is None:
            ps[str(t)] = 0.3 * (1.0 - i / len(cand))
    chosen = sorted(cand, key=lambda t: -ps[str(t)])[:keep]
    chosen = [t for t in chosen if ps[str(t)] >= 0.1] or chosen[:3]
    # level 2: windows of the kept tasks' matching chunks
    wins, texts = [], {}
    for tid in chosen:
        head = f"[{_label(meta, tid)[:160]}]"
        for nid in by_task[tid][:per_task]:
            texts[nid] = _text(db, nid)
            for off, w in _windows(texts[nid]):
                wins.append((f"{nid}:{off}", f"{head}\n{w}"))
    pw = _noul(wins, "Does this passage state the answer, or a key part of it, to this question: "
                     f"{question}", deadline=deadline)
    pw = {k: (min_p if p is None else p) for k, p in pw.items()}  # unscored windows stay, ranked last
    best = {}
    for key, p in pw.items():
        nid, off = (int(x) for x in key.split(":"))
        if nid not in best or p > best[nid][0]:
            best[nid] = (p, off)
    out = []
    for nid, (p, off) in sorted(best.items(), key=lambda kv: -kv[1][0]):
        if p < min_p or len(out) >= max_passages:
            break
        t = texts[nid]
        nxt = pw.get(f"{nid}:{off + 380}")
        ex = t[off:off + (900 if nxt is not None and nxt >= min_p else 520)].strip()
        ex = ("…" if off else "") + ex + ("…" if off + len(ex) < len(t) else "")
        out.append(_result(meta, nid, p, ex))
    return out


def _excerpt(text, question, width=600):
    kws = keywords(question)
    low = text.lower()
    pos = min((low.find(k) for k in kws if low.find(k) >= 0), default=0)
    start = max(0, pos - width // 3)
    return ("…" if start else "") + text[start:start + width].strip() + ("…" if start + width < len(text) else "")


def _days_ago(n):
    import time
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))


# ---- "seen this error before?" -------------------------------------------------------------------
_ERR_STRONG = [
    (5, re.compile(r"^\s*(?:[\w.]+\.)?[A-Z]\w*(?:Error|Exception|Exit|Interrupt)\b(?::|$)")),   # Python, JS
    (5, re.compile(r"\berror(?:\[\w+\]|\s+TS\d+)?:\s*\S")),                                   # rustc, tsc, gcc
    (5, re.compile(r"^\s*(?:fatal|FATAL|panic|PANIC)\b|panicked at|^\s*npm ERR!|^\s*E\s{2,}\w")),
    (4, re.compile(r"(?i)command not found|no such file or directory|permission denied|cannot find module|"
                   r"no module named|connection refused|could not resolve|unable to|not found:|"
                   r"syntax error|unmatched|is not recognized|segmentation fault|killed|out of memory|"
                   r"address already in use|timed out|authentication failed|unauthorized|forbidden|"
                   r"certificate (?:verify|has expired)|access denied|refused|failed to \w+|"
                   r"cannot (?:find|open|access|import|connect|read|write|create|allocate|stat|execute)|"
                   r"can't (?:open|find|connect)")),
    (3, re.compile(r"(?i)\berror\b|\bfailed\b|\bFAILED\b|\bfailure\b")),
]


def key_line(text):
    """The line of a failed command's output that names the error (None when nothing looks like one:
    a grep that found nothing, a test run whose only news is 'exit 1')."""
    if not text or text.lstrip().startswith("<tool_use_error>"):
        return None
    lines = [ln.strip() for ln in text.splitlines()[:400]]
    if lines and re.match(r"^Exit code \d+$", lines[0]):
        lines = lines[1:]
    best, best_s = None, 0
    for i, ln in enumerate(lines):
        if not ln or len(ln) > 400 or re.match(r"^(\d+[:\t]|[-*#|>]|\*\*)", ln) or len(ln.split()) > 40:
            continue  # numbered file/grep output, markdown and prose are not error messages
        for score, rx in _ERR_STRONG:
            if rx.search(ln):
                # a traceback's last exception line beats earlier ones; otherwise the first strong line wins
                if score > best_s or (score == best_s == 5 and ln[:1].isupper() and "Error" in ln):
                    best, best_s = ln, score
                break
    if best_s < 4:
        return None
    return best[:300]


def _norm_err(line):
    """Words that identify an error, without the parts that vary between occurrences."""
    line = re.sub(r"(?:/[\w.@~+-]+)+/?", " ", line)          # paths
    line = re.sub(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-f]{7,}\b|\d+", " ", line)
    return [w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z_.]{2,}", line)]


def _sentences(after, limit=6):
    """Sentences of the narration that followed an error (where a cause or fix would be stated)."""
    out = []
    for m in re.finditer(r"CLAUDE: ([^\n]+)", after or ""):
        for t in re.split(r"(?<=[.!?])\s+", m.group(1).strip()):
            if len(t) > 25:
                out.append(t[:240])
            if len(out) >= limit:
                return out
    return out


def seen_error(error_text, cmd="", exclude=(), before=None, deadline=None, same=0.8, explains=0.7, limit=1,
               fixed=None, strict=True):
    """Earlier occurrences of the same error in other sessions, with the sentence that explained or fixed it.
    Returns [{p_same, p_fix, when, project, source, fix, key}] (confident matches only)."""
    if fixed is not None:
        explains = fixed
    key = key_line(error_text)
    if not key and not strict and "\n" not in error_text.strip():
        key = error_text.strip()[:300]  # a line typed at the CLI: take it as given
    if not key:
        return []
    db = index.connect(readonly=True)
    if db is None:
        return []
    words = list(dict.fromkeys(w for w in _norm_err(key) if w not in STOP))
    if len(words) < 2:
        return []
    expr = " OR ".join('"' + w.replace('"', "") + '"' for w in words[:12])
    try:
        rows = db.execute("select err, cmd, after, path, line, ts, project, bm25(errs) s from errs where errs match ? "
                          "order by s limit 80", (expr,)).fetchall()
    except Exception:
        return []
    cands, seen_keys = [], set()
    for err, pcmd, after, path, line, ts, proj, _ in rows:
        if exclude and any(x in path for x in exclude):
            continue
        if before and ts and ts >= before:
            continue
        k2 = key_line(err)
        sents = _sentences(after)
        if not k2 or not sents:
            continue
        overlap = len(set(words) & set(_norm_err(k2))) / max(1, len(set(words)))
        if overlap < 0.5 or (k2, sents[0]) in seen_keys:
            continue
        seen_keys.add((k2, sents[0]))
        cands.append({"key": k2, "cmd": pcmd, "sents": sents, "path": path, "line": line, "ts": ts,
                      "project": proj})
        if len(cands) >= 5:
            break
    if not cands:
        return []
    state = {"current": {"command": secretscan.redact(cmd[:300]), "error": secretscan.redact(key)}, "earlier": {}}
    qs = {}
    for i, c in enumerate(cands):
        state["earlier"][f"e{i}"] = {"command": c["cmd"][:300], "error": c["key"],
                                     "said next": {f"s{j}": t for j, t in enumerate(c["sents"])}}
        qs[f"same{i}"] = {"type": "noul", "instructions":
                          f'Is earlier["e{i}"].error the same error as current.error, with the same likely cause '
                          f'(not merely the same kind of error, and not a different missing file, module, name or '
                          f'test)?'}
        for j in range(len(c["sents"])):
            qs[f"x{i}_{j}"] = {"type": "noul", "instructions":
                               f'Does earlier["e{i}"]["said next"]["s{j}"] state why earlier["e{i}"].error happened '
                               f'or how it was fixed, adding something not already obvious from the error message '
                               f'itself?'}
    try:
        ans = _hedged(lambda: jev.call(state, qs, deadline=deadline), total=deadline)
    except jev.JevError:
        return []
    out = []
    for i, c in enumerate(cands):
        ps = ans[f"same{i}"]["noul"]
        px, j = max((ans[f"x{i}_{j}"]["noul"], j) for j in range(len(c["sents"])))
        if ps >= same and px >= explains:
            out.append({"p_same": round(ps, 2), "p_fix": round(px, 2), "when": (c["ts"] or "")[:10],
                        "project": c["project"], "source": f"{c['path']}:{c['line']}", "fix": c["sents"][j],
                        "key": c["key"]})
    out.sort(key=lambda r: -(r["p_same"] + r["p_fix"]))
    return out[:limit]


def on_tool_failure(inp, deadline=4.0):
    """PostToolUseFailure (Bash) handler: one short line pointing at how the same error was handled before.
    Returns the hook output dict, or None (silent) when there's no confident match."""
    if (inp.get("tool_name") or "") != "Bash" or inp.get("is_interrupt"):
        return None
    if config.load().get("recall", {}).get("seen_error", "on") == "off":
        return None
    err = inp.get("error") or ""
    if not isinstance(err, str):
        err = json.dumps(err)
    cmd = (inp.get("tool_input") or {}).get("command", "")
    sid = inp.get("session_id") or "-"
    res = seen_error(err, cmd, exclude=(sid,) if len(sid) >= 16 else (), deadline=deadline)
    _log("seen_error", {"session": sid, "key": key_line(err), "hit": bool(res),
                        "source": res[0]["source"] if res else None})
    if not res:
        return None
    r = res[0]
    line = (f"tokenmaxxing: this error came up before ({r['when']}, {r['project']}): {r['fix']} "
            f"— details: tokenmaxxing recall --at {r['source']}")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure", "additionalContext": line}}


def _log(name, rec):
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.STATE_DIR / f"{name}.jsonl", "a") as fh:
            fh.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")
    except Exception:
        pass


def show_at(ref, width=1800):
    """`tokenmaxxing recall --at PATH:LINE`: the indexed (redacted) excerpt at a pointer, not the whole transcript."""
    path, _, line = ref.rpartition(":")
    if not line.isdigit():
        path, line = ref, "0"
    db = index.connect(readonly=True)
    if db is None:
        return "recall: no index yet"
    rows = db.execute("select n.id, n.line, n.ts from nodes n where n.path=? and n.level='chunk' and n.line<=? "
                      "order by n.line desc, n.id desc limit 2", (path, int(line))).fetchall()
    rows += db.execute("select n.id, n.line, n.ts from nodes n where n.path=? and n.level='chunk' and n.line>? "
                       "order by n.line, n.id limit 1", (path, int(line))).fetchall()
    rows.sort(key=lambda r: (r[1], r[0]))
    err = db.execute("select err, after from errs where path=? and line=?", (path, int(line))).fetchone()
    out = []
    if err:
        out.append(f"ERROR: {err[0][:800]}\nTHEN:\n{err[1]}")
    out += [_text(db, nid) for nid, _, _ in rows]
    return ("\n---\n".join(out))[:width * 2] or "recall: nothing indexed at that pointer"


# ---- "don't redo work" ------------------------------------------------------------------------------
def codex_brief(cmd, cwd=None, max_chars=20_000):
    """The brief a `codex exec` Bash command hands Codex (from `- < file`, `cat file |`, a heredoc or the prompt
    argument), or None when the command doesn't start a Codex task."""
    if not re.search(r"\bcodex\b[^|;&\n]*\bexec\b", cmd) or re.search(r"\bexec\s+(resume|--help|help)\b", cmd):
        return None
    tail = cmd[re.search(r"\bcodex\b[^|;&\n]*\bexec\b", cmd).start():]
    m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\s*\1\b", cmd, re.S)
    if m:
        return m.group(2)[:max_chars]
    m = re.search(r"(?<![<\d])<\s*(['\"]?)([^\s'\"<;|&)]+)\1", tail) or \
        re.search(r"\bcat\s+(['\"]?)([^\s'\"|;&]+)\1\s*\|[^|]*\bcodex\b", cmd)
    if m:
        path = os.path.expanduser(m.group(2).replace("$HOME", str(config.HOME)).replace("${HOME}", str(config.HOME)))
        if not os.path.isabs(path) and cwd:
            path = os.path.join(cwd, path)
        try:
            with open(path, errors="replace") as fh:
                return fh.read(max_chars)
        except OSError:
            return None
    quoted = [q[1] for q in re.findall(r"(['\"])((?:(?!\1).){60,})\1", tail, re.S)]
    return max(quoted, key=len)[:max_chars] if quoted else None


def _brief_head(text, n=1500):
    """The part of a brief that says what the task is: skip boilerplate lines (AGENTS.md preambles, blank)."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines)[:n]


def similar_launches(brief, exclude=(), before=None, days=30, deadline=None, threshold=0.85, known_text="",
                     limit=1):
    """Recent Claude subagent runs and Codex tasks that asked for the same deliverable as `brief`.
    known_text: the current session's transcript tail; earlier launches whose brief appears in it are ones
    this session started itself (it already knows), so they are skipped."""
    db = index.connect(readonly=True)
    if db is None or not brief or len(brief.strip()) < 40:
        return []
    kws = [k for k in keywords(_brief_head(brief, 3000)) if not k.isdigit()]
    if len(kws) < 3:
        return []

    def df(k):
        try:
            return db.execute("select count(*) from launches where launches match ?", ('"' + k.replace('"', "") + '"',)
                              ).fetchone()[0]
        except Exception:
            return 0
    total = db.execute("select count(*) from launches").fetchone()[0] or 1
    ranked = sorted((k for k in dict.fromkeys(kws[:40])), key=df)
    terms = [k for k in ranked if 0 < df(k) <= 0.05 * total][:16] or [k for k in ranked if df(k) > 0][:8]
    if not terms:
        return []
    expr = " OR ".join('"' + k.replace('"', "") + '"' for k in terms)
    since = _days_ago(days) if days else ""
    rows = db.execute("select brief, kind, path, line, ts, project, result, bm25(launches) s from launches "
                      "where launches match ? order by s limit 60", (expr,)).fetchall()
    head = secretscan.redact(_brief_head(brief))  # the new brief goes to Jev: never with a key in it
    cands = []
    for b, kind, path, line, ts, proj, result, _ in rows:
        if exclude and any(x in path for x in exclude):
            continue
        if (before and ts and ts >= before) or (since and ts and ts[:10] < since):
            continue
        bh = _brief_head(b)
        if known_text and re.sub(r"\s+", " ", bh)[:120] in known_text:
            continue
        cands.append({"brief": bh, "kind": kind, "path": path, "line": line, "ts": ts, "project": proj,
                      "result": result or ""})
        if len(cands) >= 8:
            break
    if not cands:
        return []
    items = [(str(i), f"EARLIER TASK ({c['kind']}, {c['ts'][:10]}, {c['project']}):\n{c['brief']}")
             for i, c in enumerate(cands)]
    ps = _noul(items, "The NEW TASK brief is in context. Does this EARLIER TASK ask for the same deliverable with the "
                      "same goal (not just the same project, files or topic), so that its result could be reused "
                      "instead of doing the new task again?", deadline=deadline, context={"NEW TASK": head})
    out = []
    for i, c in enumerate(cands):
        p = ps.get(str(i))
        if p is not None and p >= threshold:
            out.append({**c, "p": round(p, 2)})
    out.sort(key=lambda r: -r["p"])
    return out[:limit]


def _visible_text(path, stop_at=None, max_bytes=4_000_000):
    """What the current session can still see: its transcript since the last compaction, whitespace-collapsed
    (an earlier launch found here is one the agent already knows about, so pointing at it would be noise)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    if stop_at:  # the pending call itself is already in the transcript; what counts is what came before it
        j = data.find(stop_at)
        if j >= 0:
            data = data[:data.rfind("\n", 0, j) + 1]
    k = data.rfind('"subtype":"compact_boundary"')
    if k >= 0:
        data = data[k:]
    data = data.replace("\\n", " ").replace("\\t", " ").replace('\\"', '"')
    return re.sub(r"\s+", " ", data)


def on_launch(inp, deadline=4.0):
    """PreToolUse handler for Agent (not forks) and for Bash `codex exec`: one pointer line when a recent task
    asked for the same deliverable. Returns the hook output dict, or None (silent)."""
    if config.load().get("recall", {}).get("dedup", "on") == "off":
        return None
    ti = inp.get("tool_input") or {}
    tool = inp.get("tool_name")
    if tool == "Agent":
        if ti.get("subagent_type") == "fork":
            return None
        brief = ti.get("prompt") or ""
    elif tool == "Bash":
        brief = codex_brief(ti.get("command", ""), inp.get("cwd"))
    else:
        return None
    if not brief:
        return None
    sid = inp.get("session_id") or "-"
    # Not excluding this session's own earlier launches: after a compaction the agent no longer sees them,
    # which is when a relaunch is most likely a redo. Launches it can still see are skipped via known_text.
    res = similar_launches(brief, deadline=deadline, known_text=_visible_text(inp.get("transcript_path") or "",
                                                                     stop_at=inp.get("tool_use_id")))
    _log("dedup", {"session": sid, "tool": tool, "hit": bool(res), "p": res[0]["p"] if res else None,
                   "source": f"{res[0]['path']}:{res[0]['line']}" if res else None})
    if not res:
        return None
    r = res[0]
    what = re.sub(r"\s+", " ", r["brief"])[:110]
    got = re.sub(r"\s+", " ", r["result"])[:160]
    line = (f"tokenmaxxing: a similar task already ran ({r['kind']}, {r['ts'][:10]}, {r['project']}): \"{what}…\""
            + (f" Result: {got}…" if got else "")
            + f" Check it before redoing the work: tokenmaxxing recall --at {r['path']}:{r['line']}")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": line}}


def main(args):
    if not args:
        sys.exit('usage: tokenmaxxing recall "QUESTION" [--days N] [--json] | tokenmaxxing recall --error "ERROR LINE" | '
                 'tokenmaxxing recall --at PATH:LINE')
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    if args[0] == "--at":
        print(show_at(args[1]))
        return
    if args[0] == "--error":
        res = seen_error(" ".join(args[1:]), same=0.5, explains=0.5, limit=3, strict=False)
    else:
        days = None
        if "--days" in args:
            i = args.index("--days")
            days = int(args[i + 1])
            del args[i:i + 2]
        res = search(" ".join(args), days=days)
    if as_json:
        print(json.dumps(res, indent=1))
        return
    if not res:
        print("recall: nothing relevant found")
    for r in res:
        if "fix" in r:
            print(f"- [{r['when']}] {r['project']}  ({r['source']})  same {r['p_same']}, explains {r['p_fix']}")
            print(f"  error: {r['key']}\n  then: {r['fix']}")
            continue
        print(f"- [{r.get('when', '')}] {r.get('session', '')}  ({r.get('source', '')})")
        if r.get("task"):
            print(f"  task: {r['task']}")
        print("  " + (r.get("excerpt") or r.get("then", "")).replace("\n", "\n  "))
