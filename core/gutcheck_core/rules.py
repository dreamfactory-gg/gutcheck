"""Standing rules captured as they are stated, so they survive compaction at ~100%.

Each owner message (and each team-lead message to a teammate) is split into units and classified by Jev with a
recall-oriented question: explicit rules and implicit ones (preferences, constraints, boundaries, budget,
availability or schedule facts) all count. Rules go to a per-session store:

  $GUTCHECK_RULES_DIR/<session>.rules.jsonl   every unit seen (rule, not a rule, or pending)
  $GUTCHECK_RULES_DIR/<session>.rules.md      the rules, verbatim, rendered from the store

The store is shared by both plugins (default ~/.local/share/gutcheck/rules): gutcheck's don't-delete guard and
tokenmaxxing's checkpoint read it, and when both capture the same message the second finds its units already
stored (same key, under the lock), so Jev classifies each message once.

Exact and near-duplicate restatements collapse to one line (latest wording). A later rule that clearly replaces an
earlier one marks the earlier one superseded; both lines stay. The checkpoint reads this store first and renders
it outside its size cap.

Entry points (all fail open):
  on_user_prompt(inp)   UserPromptSubmit: capture the owner's message (Jev deadline ~1.5 s; pending if it misses)
  on_stop(inp)          Stop: incremental transcript catch-up (queued prompts, lead messages to a teammate)
  sync(session, transcript_path, until_line=None, deadline=...)   PreCompact: catch up + classify anything pending
"""
import json
import os
import re
import time
from contextlib import contextmanager

from . import config
from . import messages as msg
from .lock import locked

RULE_CONDITION = (
    "Does this message from the owner tell the agent anything that should still shape its behaviour later in the "
    "session: a rule, preference, constraint, boundary, permission, budget, availability or schedule fact, or a "
    "decision about how to work? Answer no only for pure questions, one-off requests already handled, and pasted "
    "logs or third-party text.")
LEAD_CONDITION = (
    "A team lead wrote this to a teammate agent. Does it direct how the teammate must work from now on (a must, "
    "must-not, keep, stop, don't, only, report-how, confirm-before, or a decision the teammate must follow), rather "
    "than explain, analyse, report findings, or describe the task?")
# Lead sentences that read as instructions are kept verbatim whatever Jev says; Jev only decides the rest and ranks
# everything for the budget. Imperative openers or modal/constraint words.
LEAD_CUE = re.compile(
    r"(?i)(^\W*(do not|don't|never|always|keep|stop|report|say|state|write|run|use|check|record|put|flag|name|treat|"
    r"make|let|skip|include|end|answer|quote|recommend|drop|hold|send|propose|confirm|follow|reuse|derive|add|land|"
    r"build|ask|finish|measure|intervene|verify|then)\b)|\b(must|must not|do not|don't|never|always|only if|only|"
    r"unless|should|shall|without asking|before you|until|confirm with|ask me|wait for|no need|not allowed|"
    r"forbidden|required|keep it|leave it|let it)\b")
LEAD_MIN_P = 0.3            # with LEAD_CUE: recall first; Jev's score still orders what fits the lead budget
RULE_MIN_P = 0.5            # 250+83 hand-labeled units: owner 38/38, lead 59/59 at this threshold (in-sample)
SUPERSEDE_MIN_P = 0.7       # "clearly replaces": precision-oriented
PROMPT_DEADLINE = 1.5
SYNC_DEADLINE = 25.0
LEAD_CAP_TOKENS = 45000     # lead section budget: most certain first, shown oldest first, rest via the rules file
_STOP = set("the a an and or but to of in on for with is are was be it this that we i you my our your me us at as "
            "by from if so not no do dont don't can will just all any".split())


def _paths(session):
    base = config.RULES_DIR / re.sub(r"[^\w.-]", "_", str(session or "unknown"))
    return (base.with_name(base.name + ".rules.jsonl"), base.with_name(base.name + ".rules.md"),
            base.with_name(base.name + ".rules.state.json"), base.with_name(base.name + ".rules.lock"))


@contextmanager
def _locked(session):
    store, _, _, lock = _paths(session)
    store.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as fh, locked(fh):
        yield


def load(session):
    store = _paths(session)[0]
    out = []
    try:
        for line in store.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def _save(session, recs):
    store, md, _, _ = _paths(session)
    tmp = store.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in recs))
    tmp.replace(store)
    tmp = md.with_suffix(".tmp")
    tmp.write_text(render(recs, lead_cap=None) or "(no standing rules yet)\n")
    tmp.replace(md)


def key(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


_WORDS = {}


def _words(text):
    w = _WORDS.get(text)
    if w is None:
        w = _WORDS[text] = frozenset(x for x in re.findall(r"[a-z0-9][a-z0-9'./_-]{2,}", text.lower())
                                     if x not in _STOP)
    return w


def _near_dup(a, b):
    ka, kb = key(a), key(b)
    if not ka or not kb:
        return False
    if ka != kb and set(re.findall(r"\d+", a)) != set(re.findall(r"\d+", b)):
        return False          # "module 3" and "module 4" are different rules
    if ka == kb or (min(len(ka), len(kb)) >= 25 and (ka in kb or kb in ka)):
        return True
    wa, wb = _words(a), _words(b)
    return len(wa) >= 4 and len(wb) >= 4 and len(wa & wb) / len(wa | wb) >= 0.8


def _related(a, b):
    wa, wb = _words(a), _words(b)
    inter = len(wa & wb)
    return inter >= 2 and inter / max(1, min(len(wa), len(wb))) >= 0.25, inter


# ---- adding units -------------------------------------------------------------------------------------
def _new_units(recs, ts, src, text, cwd=None):
    """Units of a message not already in the store (by exact key). A don't-delete unit also records the existing
    paths it names, resolved against the message's cwd (keep_hit)."""
    from . import secretscan
    seen = {r["key"] for r in recs} | {k for r in recs for k in r.get("merged_keys", [])}
    out = []
    for u in msg.units(msg._clean(text)):
        u = secretscan.redact(u)
        k = key(u)
        if k and k not in seen:
            seen.add(k)
            out.append({"key": k, "ts": ts, "first_ts": ts, "src": src, "text": u, "status": "pending", "p": None})
            keep, but = _keep_paths(u, cwd)
            if keep:
                out[-1].update(keep=keep, keep_except=but)
    return out


def _classify(recs, pending, deadline):
    """One Jev request: is each pending unit a rule. Mutates the records."""
    from . import jev
    todo = pending[:jev.MAX_ITEMS]
    if not todo:
        return "none"
    items = {f"u{i}": r["text"][:jev.ITEM_CHARS] for i, r in enumerate(todo)}
    qs = {f"r{i}": {"type": "noul", "instructions": f'Consider only items["u{i}"]. '
                    f'{LEAD_CONDITION if r["src"] == "lead" else RULE_CONDITION}'} for i, r in enumerate(todo)}
    try:
        if not jev.have_key():
            raise jev.JevError("no key")
        ans = jev.call({"items": items}, qs, deadline=deadline)
    except Exception:
        return "failed"
    for i, r in enumerate(todo):
        a = ans.get(f"r{i}") or {}
        if "noul" in a:
            r["p"] = float(a["noul"])
            if r["src"] == "lead":
                r["status"] = "rule" if r["p"] >= LEAD_MIN_P or LEAD_CUE.search(r["text"]) else "no"
            else:
                r["status"] = "rule" if r["p"] >= RULE_MIN_P else "no"
    return "jev"


def _supersede(recs, new, deadline):
    """For each newly classified rule, ask whether it clearly replaces a related earlier active rule (same source).
    The earlier one is marked superseded; both stay. One Jev request for all pairs."""
    from . import jev
    new_ids = {id(r) for r in new}
    order = sorted(recs, key=lambda r: r["ts"] or "")
    pairs = []
    for r in new:
        if r["status"] != "rule":
            continue
        earlier = [o for o in order if o is not r and o["status"] == "rule" and not o.get("superseded_by")
                   and o["src"] == r["src"] and (o["ts"] or "") <= (r["ts"] or "")
                   and (id(o) not in new_ids or order.index(o) < order.index(r))]
        cands = sorted(((rel[1], o) for o in earlier for rel in [_related(r["text"], o["text"])] if rel[0]),
                       key=lambda x: -x[0])[:3]
        pairs += [(r, o) for _, o in cands]
    pairs = pairs[:jev.MAX_ITEMS]
    if not pairs:
        return "none"
    items, qs = {}, {}
    for n, (r, o) in enumerate(pairs):
        items[f"old{n}"], items[f"new{n}"] = o["text"][:jev.ITEM_CHARS], r["text"][:jev.ITEM_CHARS]
        qs[f"s{n}"] = {"type": "noul", "instructions":
                       f'items["old{n}"] is an earlier instruction and items["new{n}"] a later message from the same '
                       f'person. Does the later message clearly replace, cancel or change the earlier instruction, so '
                       f'that the earlier one no longer applies as written?'}
    try:
        ans = jev.call({"items": items}, qs, deadline=deadline)
    except Exception:
        return "failed"
    for n, (r, o) in enumerate(pairs):
        if float((ans.get(f"s{n}") or {}).get("noul", 0)) >= SUPERSEDE_MIN_P and not o.get("superseded_by"):
            o["superseded_by"] = r["ts"]
            o["superseded_text"] = r["text"][:120]
    return "jev"


def _merge_dups(recs, new):
    """Near-duplicate restatements: a newly classified rule that restates an active earlier one replaces its wording
    (the latest wins; the first date and the merged keys are kept) instead of adding a line."""
    new_ids = {id(r) for r in new}
    active = [o for o in recs if id(o) not in new_ids and o["status"] == "rule" and not o.get("superseded_by")]
    drop = set()
    for r in new:
        if r["status"] != "rule":
            continue
        for o in active:
            if o["src"] == r["src"] and _near_dup(o["text"], r["text"]):
                o.setdefault("restated", []).append(o["ts"])
                o["text"], o["ts"], o["p"] = r["text"], r["ts"], max(o.get("p") or 0, r.get("p") or 0)
                o.setdefault("merged_keys", []).append(r["key"])
                o.update({k: r[k] for k in ("keep", "keep_except") if k in r})
                drop.add(id(r))
                break
        else:
            active.append(r)
    return [r for r in recs if id(r) not in drop]


def add_message(session, ts, src, text, deadline=PROMPT_DEADLINE, cwd=None):
    """Record one owner/lead message. Returns how it was classified ('jev', 'failed', 'none')."""
    with _locked(session):
        recs = load(session)
        new = _new_units(recs, ts, src, text, cwd)
        if not new:
            return "none"
        recs.extend(new)
        _save(session, recs)  # stored as pending first: the prompt hook may exit before Jev answers
        t0 = time.monotonic()
        how = _classify(recs, new, deadline)
        recs = _merge_dups(recs, new)
        left = deadline - (time.monotonic() - t0)
        if how == "jev" and left > 0.3:
            _supersede(recs, [r for r in new if any(r is x for x in recs)], left)
        _save(session, recs)
    return how


# ---- transcript catch-up ------------------------------------------------------------------------------
def _messages(path, start=0, until_line=None):
    """(ts, src, text, cwd) of owner/lead messages from byte offset `start`; returns (messages, end_offset)."""
    msgs = []
    try:
        fh = open(path, "rb")
    except OSError:
        return msgs, start
    with fh:
        fh.seek(start)
        n = 0
        pos = start
        for raw in fh:
            if until_line is not None and n >= until_line:
                break
            n += 1
            if not raw.endswith(b"\n"):
                break          # a line still being written: pick it up next time
            pos += len(raw)
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("isSidechain"):
                continue
            t = d.get("type")
            texts = []
            if t == "user" and not (d.get("isMeta") or d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly")):
                texts = msg._texts((d.get("message") or {}).get("content"))
            elif t == "attachment":
                a = d.get("attachment") or {}
                if a.get("type") == "queued_command" and a.get("commandMode", "prompt") == "prompt":
                    texts = msg._texts(a.get("prompt"))
            for txt in texts:
                src = msg.owner_source(txt)
                if src:
                    msgs.append((d.get("timestamp"), src, txt, d.get("cwd")))
    return msgs, pos


def sync(session, transcript_path, until_line=None, deadline=SYNC_DEADLINE):
    """Catch up from the transcript and classify every pending unit. Returns a short status dict."""
    from concurrent.futures import ThreadPoolExecutor
    _, _, statep, _ = _paths(session)
    t0 = time.monotonic()
    with _locked(session):
        try:
            state = json.loads(statep.read_text())
        except Exception:
            state = {}
        start = 0 if until_line is not None or state.get("transcript") != transcript_path else state.get("offset", 0)
        msgs, end = _messages(transcript_path, start, until_line)
        recs = load(session)
        for ts, src, text, cwd in msgs:
            recs.extend(_new_units(recs, ts, src, text, cwd))
        pending = [r for r in recs if r["status"] == "pending"]
        chunks = [pending[i:i + 60] for i in range(0, len(pending), 60)]
        # classify in parallel chunks, then one supersession pass over everything new
        left = max(0.5, deadline - (time.monotonic() - t0))
        with ThreadPoolExecutor(8) as ex:
            hows = list(ex.map(lambda c: _classify(recs, c, left), chunks))
        recs = _merge_dups(recs, pending)
        left = max(0.5, deadline - (time.monotonic() - t0))
        kept = {id(x) for x in recs}
        _supersede(recs, [r for r in pending if id(r) in kept], left)
        _save(session, recs)
        if until_line is None:
            statep.write_text(json.dumps({"transcript": transcript_path, "offset": end}))
    return {"messages": len(msgs), "pending_before": len(pending),
            "pending_after": sum(1 for r in recs if r["status"] == "pending"), "jev": hows}


# ---- hook handlers ------------------------------------------------------------------------------------
def _log(rec):
    try:
        from . import hookio
        hookio.log("rules", rec)
    except Exception:
        pass


def on_user_prompt(inp):
    """UserPromptSubmit. Never raises; never prints."""
    try:
        prompt = inp.get("prompt") or ""
        src = msg.owner_source(prompt)
        if not src or not inp.get("session_id"):
            return
        t0 = time.monotonic()
        how = add_message(inp["session_id"], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), src, prompt,
                          cwd=inp.get("cwd"))
        _log({"session": inp.get("session_id"), "via": "prompt", "src": src, "chars": len(prompt), "how": how,
              "ms": int((time.monotonic() - t0) * 1000)})
    except Exception as e:
        _log({"session": inp.get("session_id"), "via": "prompt", "error": f"{type(e).__name__}: {e}"})


def on_stop(inp, deadline=PROMPT_DEADLINE):
    """Stop: pick up messages UserPromptSubmit never saw (queued prompts, the lead's messages to a teammate)."""
    try:
        if not inp.get("session_id") or not inp.get("transcript_path"):
            return
        t0 = time.monotonic()
        st = sync(inp["session_id"], inp["transcript_path"], deadline=deadline)
        if st["messages"]:
            _log({"session": inp.get("session_id"), "via": "stop", **st, "ms": int((time.monotonic() - t0) * 1000)})
    except Exception as e:
        _log({"session": inp.get("session_id"), "via": "stop", "error": f"{type(e).__name__}: {e}"})


# ---- rendering ----------------------------------------------------------------------------------------
def _tag(r):
    if r.get("superseded_by"):
        return f"(superseded by the {msg._fmt_ts(r['superseded_by'])} instruction) "
    return "(not yet checked) " if r["status"] == "pending" else ""


def _line(r):
    who = "lead" if r["src"] == "lead" else "owner"
    body = r["text"].strip().replace("\n", "\n  ")
    return f"- [{msg._fmt_ts(r['ts'])} {who}] {_tag(r)}{body}"


def _plausible(text, src="owner"):
    if src == "lead":
        return bool(LEAD_CUE.search(text))
    return bool(msg._HEUR.search(text)) or (len(text) <= 240 and not re.search(r"\bat \S+:\d+|^\S+:\d+|https?://\S+$", text))


def render(recs, lead_cap=LEAD_CAP_TOKENS, pointer=None):
    """Markdown for the standing-rules sections: owner rules in full, then the lead's instructions."""
    # pending = Jev never answered: keep it (recall first) unless it looks like a pasted log line
    keep = [r for r in recs if r["status"] == "rule" or (r["status"] == "pending" and _plausible(r["text"], r["src"]))]
    keep.sort(key=lambda r: r["ts"] or "")
    owner = [_line(r) for r in keep if r["src"] != "lead"]
    lead_recs = [r for r in keep if r["src"] == "lead"]
    out = []
    if owner:
        out += ["## Standing instructions from the owner (verbatim, oldest first; all still apply unless marked "
                "superseded)"] + owner
    if lead_recs:
        body = lambda r: "- " + _tag(r) + r["text"].strip().replace("\n", "\n  ")   # noqa: E731
        chosen, dropped = set(range(len(lead_recs))), 0
        if lead_cap and msg.est_tokens("\n".join(body(r) for r in lead_recs)) > lead_cap:
            # the lead rules that fit, shown in time order; the rest stay in the rules file
            # rank: Jev's score, a bonus for instruction-shaped sentences, per sqrt(tokens) so the budget holds
            # more distinct rules (tuned on the 3b318ea1 labels: 34/38 at 25k vs 26/38 by score alone)
            def rank(r):
                sc = (r.get("p") or LEAD_MIN_P) + (0.1 if LEAD_CUE.search(r["text"]) else 0)
                return sc / msg.est_tokens(r["text"]) ** 0.5
            order = sorted(range(len(lead_recs)), key=lambda i: (-rank(lead_recs[i]), -i))
            chosen, used = set(), 60
            for i in order:
                c = msg.est_tokens(body(lead_recs[i])) + 1 + 4      # + a share of the per-message headers
                if used + c <= lead_cap:
                    chosen.add(i)
                    used += c
            dropped = len(lead_recs) - len(chosen)
        out += ["", "## The lead's instructions to this teammate (verbatim, grouped by message, oldest first)"]
        if dropped:
            out.append(f"({dropped} more lead instructions in {pointer or 'the rules file'})")
        last = None
        for i, r in enumerate(lead_recs):
            if i not in chosen:
                continue
            if r["ts"] != last:
                out.append(f"### lead, {msg._fmt_ts(r['ts'])}")
                last = r["ts"]
            out.append(body(r))
    return "\n".join(out) + ("\n" if out else "")


def section(session, lead_cap=LEAD_CAP_TOKENS):
    """(markdown, meta) for the checkpoint."""
    recs = load(session)
    md = render(recs, lead_cap=lead_cap, pointer=str(_paths(session)[1]))
    shown = [r for r in recs if r["status"] == "rule" or (r["status"] == "pending" and _plausible(r["text"], r["src"]))]
    owner = [r for r in shown if r["src"] != "lead"]
    lead = [r for r in shown if r["src"] == "lead"]
    return md, {"owner_rules": len(owner), "lead_rules": len(lead),
                "superseded": sum(1 for r in recs if r.get("superseded_by")),
                "pending": sum(1 for r in recs if r["status"] == "pending"),
                "owner_tokens": msg.est_tokens("\n".join(_line(r) for r in owner)) if owner else 0,
                "lead_tokens_all": msg.est_tokens(render(lead, None)) if lead else 0,
                "lead_tokens": msg.est_tokens(render([r for r in recs if r["src"] == "lead"], lead_cap)) if lead else 0,
                "rules_tokens": msg.est_tokens(md) if md else 0}


# ---- don't-delete guard -------------------------------------------------------------------------------
# "don't delete X", "never overwrite `X`": the clause after the verb, up to the end of its sentence; paths after
# "except"/"but" in it are exclusions
_KEEP = re.compile(r"(?i)\b(?:don'?t|do not|never|must not|mustn'?t|should ?n[o']t)\s+(?:\w+\s+){0,2}?"
                   r"(?:delete|remove|rm|overwrite|wipe|erase|clobber|trash)\b((?:[^.!?;\n]|\.(?=[\w/]))*)")
_KEEP_BUT = re.compile(r"(?i)\b(?:except|but|other than|apart from|besides)\b")


def _keep_paths(text, cwd):
    """(keep, except): existing paths a don't-delete instruction names, resolved against the cwd it was given in.
    Path-shaped words only (a '/' or a file extension); '/' itself never counts."""
    keep, but = [], []
    for m in _KEEP.finditer(text) if cwd else []:
        for n, part in enumerate(_KEEP_BUT.split(m.group(1), maxsplit=1)):
            for w in re.findall(r"[~\w./-]+", part):
                w = w.rstrip(".")
                p = os.path.realpath(os.path.join(cwd, os.path.expanduser(w)))
                if ("/" in w or re.search(r"\w\.\w+$", w)) and p != "/" and os.path.lexists(p):
                    (but if n else keep).append(p)
    return keep, but


def keep_hit(session, paths):
    """(instruction, path) when a standing don't-delete instruction covers one of `paths` (absolute, resolved;
    fnmatch patterns from `find -delete`): the same path, a path inside it, or a directory holding it while it
    still exists. Superseded instructions don't count; Jev's rule call doesn't matter (the wording states the
    boundary)."""
    from fnmatch import fnmatch
    for r in load(session) if session and paths else []:
        if r.get("superseded_by"):
            continue
        for keep in r.get("keep", []):
            for p in paths:
                if any(p == x or p.startswith(x + "/") for x in r.get("keep_except", [])):
                    continue
                if (fnmatch(keep, p) if any(ch in p for ch in "*?[") else
                        p == keep or p.startswith(keep + "/") or
                        keep.startswith(p.rstrip("/") + "/") and os.path.lexists(keep)):
                    return r["text"], keep
    return None
