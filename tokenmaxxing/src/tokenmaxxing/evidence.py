"""Check a report's claims against the evidence they cite (subagent reports, Codex runs).

For each claim that cites evidence (`path:line`, `path:start-end`, or a backticked command the agent actually
ran), gather the cited text and ask Jev how it relates to the claim (TypeSafe's citation-check recipe: one
Choice, supports / contradicts / says nothing, confidence-gated). Deterministic checks run first: the path
exists, the lines exist, and every number in the claim appears in the evidence.

  tokenmaxxing evidence REPORT_FILE|- [--cwd DIR] [--transcript JSONL] [--all]

SubagentStop hook: `on_subagent_stop(inp)` returns at most one short line of feedback for the subagent (it
continues once so it can correct its report) and never blocks. `[evidence] mode = "on" | "log" | "off"` (default
"on") and `classifier = "sonnet-low" | "haiku" | "codex-low" | "jev"` (default sonnet-low, one `claude -p` call per
report; see DEFAULT_CLASSIFIER for the measurement).
"""
import calendar
import json
import os
import re
import subprocess
import sys
import time

from gutcheck_core import config, jev, secretscan

LOG = config.STATE_DIR / "evidence.jsonl"
MAX_EVIDENCE_LINES = 150
CONTEXT_LINES = 3
WIDE_CONTEXT = 20          # second look before a "says nothing" can become a flag
AUTO_ACCEPT = 0.8          # below this confidence a Jev verdict is logged, never flagged
MAX_CLAIMS = 60

_EXT = r"(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|luau|lua|rs|go|rb|java|kt|swift|c|cc|cpp|h|hpp|cs|php|sh|zsh|bash|" \
       r"md|mdx|txt|json|jsonl|toml|ya?ml|ini|cfg|html|css|scss|sql|proto|gradle|xml|rst|env\.example|lock|conf)"
CITE_RE = re.compile(
    r"(?P<path>(?:~|\.{1,2})?/?(?:[\w@+.\-]+/)*[\w@+.\-]+\." + _EXT + r")"
    r"(?:(?::|#L| lines? | L)(?P<start>\d+)(?:\s*[-–:]\s*L?(?P<end>\d+))?)", re.I)
NUM_RE = re.compile(r"(?<![\w.\-/:#])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(%|k|ms|s|x|×)?(?![\w/:])")
BACKTICK_RE = re.compile(r"`([^`\n]{3,300})`")
CONT_RE = re.compile(r"(?:^|[\s,(`])(?:L|:)(\d+)(?:\s*[-–]\s*L?(\d+))?(?=[\s,.;)`]|$)")


# ---- claims ------------------------------------------------------------------------------------
def claims(report):
    """[{text, cites: [{kind: file, path, start, end} | {kind: cmd, cmd}]}] for lines/sentences that cite."""
    out, in_code = [], False
    for raw in report.splitlines():
        s = raw.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not s:
            continue
        units = [s] if len(s) <= 500 else re.split(r"(?<=[.;])\s+(?=[A-Z`(])", s)
        for u in units:
            cites = []
            for m in CITE_RE.finditer(u):
                start = int(m.group("start"))
                end = int(m.group("end") or start)
                if end < start:  # "file.py:12:3" style column, not a range
                    end = start
                cites.append({"kind": "file", "path": m.group("path"), "start": start, "end": end,
                              "span": m.span()})
            if cites:  # bare ":590" / ":590-600" after a file cite means the same file
                last = cites[-1]
                for m in CONT_RE.finditer(u):
                    if any(a <= m.start(1) < b for a, b in (c["span"] for c in cites)):
                        continue
                    prev = [c for c in cites if c["span"][1] <= m.start()]
                    if prev:
                        last = prev[-1]
                    st = int(m.group(1))
                    cites.append({"kind": "file", "path": last["path"], "start": st,
                                  "end": max(st, int(m.group(2) or st)), "span": m.span()})
            for m in BACKTICK_RE.finditer(u):
                cites.append({"kind": "cmd?", "cmd": m.group(1).strip(), "span": m.span()})
            if cites:
                out.append({"text": u, "cites": cites})
    return out


def claim_numbers(text, cites):
    """Numbers the claim asserts: outside citations, identifiers, paths, versions and dates."""
    masked = text
    for c in sorted(cites, key=lambda c: -c["span"][0]):
        if c["kind"] == "file":
            a, b = c["span"]
            masked = masked[:a] + " " * (b - a) + masked[b:]
    masked = re.sub(r"\[[^\]]*\]\([^)]*\)", " ", masked)            # markdown links: text and target are references
    masked = re.sub(r"^\s*\d+[.)]\s", " ", masked)                    # list numbering
    masked = re.sub(r"§+\s*[\d.]+(?:\s*,\s*[\d.]+)*|\b(?:lines?|L)\s*\d+(?:\s*[-–]\s*\d+)?|:\d+(?:\s*[-–]\s*\d+)?",
                    " ", masked)                                          # section and line references
    masked = re.sub(r"\b\d{4}-\d{2}-\d{2}\b|\bv?\d+\.\d+\.\d+\S*|#\d+|\b[0-9a-f]{7,40}\b", " ", masked)
    masked = re.sub(r"`[^`]*`", lambda m: " " * len(m.group()), masked)  # code spans are quotes, not numbers
    nums = []
    for m in NUM_RE.finditer(masked):
        n = m.group(1).replace(",", "")
        if n in ("0", "1", "2") and not m.group(2):
            continue  # "one of two", "0 errors" are too common to anchor on
        nums.append(n)
    return nums


def number_in(n, evidence):
    variants = {n}
    if "." not in n and len(n) > 3:
        variants.add(f"{int(n):,}")
    if n.endswith(".0"):
        variants.add(n[:-2])
    return any(re.search(rf"(?<![\d.]){re.escape(v)}(?!\d|\.\d)", evidence) for v in variants)


# ---- evidence ----------------------------------------------------------------------------------
def _repo_files(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "ls-files"], capture_output=True, text=True, timeout=5)
        root = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
        return root, r.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired):
        return None, []


def resolve(path, cwd, cache):
    p = os.path.expanduser(path)
    if os.path.isabs(p):
        return p if os.path.isfile(p) else None
    if cwd and os.path.isfile(os.path.join(cwd, p)):
        return os.path.join(cwd, p)
    if cwd not in cache:
        cache[cwd] = _repo_files(cwd) if cwd else (None, [])
    root, files = cache[cwd]
    if root and os.path.isfile(os.path.join(root, p)):
        return os.path.join(root, p)
    tail = "/" + p.lstrip("./")
    hits = [f for f in files if ("/" + f).endswith(tail)]
    return os.path.join(root, hits[0]) if root and len(hits) == 1 else None


def file_lines(path, as_of=None):
    """Lines of the file now, or as committed at `as_of` (ISO time) when it has changed since then."""
    if as_of:
        try:
            if os.path.getmtime(path) > calendar.timegm(time.strptime(as_of[:19], "%Y-%m-%dT%H:%M:%S")):
                d = os.path.dirname(path)
                root = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True,
                                      text=True, timeout=5).stdout.strip()
                if not root:
                    return None, "changed since report"
                rel = os.path.relpath(path, root)
                sha = subprocess.run(["git", "-C", root, "log", "-1", f"--before={as_of}", "--format=%H", "--", rel],
                                     capture_output=True, text=True, timeout=5).stdout.strip()
                later = subprocess.run(["git", "-C", root, "log", f"--since={as_of}", "--format=%H", "--", rel],
                                       capture_output=True, text=True, timeout=5).stdout.strip()
                dirty = subprocess.run(["git", "-C", root, "status", "--porcelain", "--", rel],
                                       capture_output=True, text=True, timeout=5).stdout.strip()
                if not sha or (not later and dirty):
                    return None, "changed since report"
                if later:
                    r = subprocess.run(["git", "-C", root, "show", f"{sha}:{rel}"], capture_output=True, timeout=5)
                    return r.stdout.decode(errors="replace").splitlines(), None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    try:
        with open(path, errors="replace") as fh:
            return fh.read().splitlines(), None
    except OSError as e:
        return None, f"unreadable: {type(e).__name__}"


def file_evidence(path, start, end, as_of=None, context=None):
    context = CONTEXT_LINES if context is None else context
    lines, err = file_lines(path, as_of)
    if lines is None:
        return None, err
    if start > len(lines):
        return None, f"line {start} is past the end of the file ({len(lines)} lines)"
    a = max(1, start - context)
    b = min(len(lines), end + context)
    idx = list(range(a, b + 1))
    if len(idx) > MAX_EVIDENCE_LINES:
        idx = idx[:100] + idx[-(MAX_EVIDENCE_LINES - 100):]
    return "\n".join(f"{n}: {lines[n - 1]}" for n in idx), None


def tool_log(transcript):
    """[(command, output)] of the Bash calls in a transcript."""
    if not transcript or not os.path.exists(transcript):
        return []
    calls, results = {}, {}
    with open(transcript, errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            c = (d.get("message") or {}).get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") == "Bash":
                    calls[b.get("id")] = (b.get("input") or {}).get("command", "")
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                    cont = b.get("content")
                    if isinstance(cont, list):
                        cont = "\n".join(x.get("text", "") for x in cont if isinstance(x, dict))
                    results[b["tool_use_id"]] = str(cont or "")
    return [(calls[k], results.get(k, "")) for k in calls]


def command_evidence(cmd, log):
    """Output of the last run whose command contains the cited text (or equals it), else None."""
    c = cmd.strip()
    if len(c) < 6 or " " not in c:
        return None
    for ran, out in reversed(log):
        segs = [x.strip() for x in re.split(r"&&|\|\||;|\||\n", ran)]
        if ran.strip() == c or any(x == c or x.startswith(c + " ") or c.startswith(x + " ") and x for x in segs):
            return f"$ {ran[:500]}\n{out[-6000:]}"
    return None


def _gather(cites, cwd, cache, log, as_of, context):
    """(evidence text, problems, cite labels) for one claim's citations."""
    ev_parts, problems, used = [], [], []
    for c in cites:
        if c["kind"] == "file":
            path = resolve(c["path"], cwd, cache)
            if not path:
                continue  # unresolved: can't check, don't guess
            ev, err = file_evidence(path, c["start"], c["end"], as_of, context)
            if err and "past the end" in err:
                problems.append(f"{c['path']}:{c['start']}: {err}")
            elif ev:
                ev_parts.append(f"## {c['path']}:{c['start']}-{c['end']}\n{ev}")
                used.append(f"{c['path']}:{c['start']}" + (f"-{c['end']}" if c["end"] != c["start"] else ""))
        else:
            ev = command_evidence(c["cmd"], log)
            if ev:
                ev_parts.append(f"## command output\n{ev}")
                used.append(f"`{c['cmd'][:60]}`")
    return "\n\n".join(ev_parts), problems, used


# ---- judging -----------------------------------------------------------------------------------
QUESTION = {"relation": {
    "type": "choice",
    "instructions": "How does the evidence relate to the claim? The evidence is the file lines or command "
                    "output the claim cites as its source (numbered lines are `line: text`).",
    "criteria": {
        "supports": "The evidence states the claim or directly implies that it is true",
        "contradicts": "The evidence states the opposite of the claim or implies it is false",
        "says_nothing": "The evidence does not address what the claim asserts, either way",
    }}}


def judge(pairs, deadline=None):
    """pairs: [(claim, evidence)] -> [{relation, confidence, probabilities}] (one request per pair, in parallel)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(pair):
        claim, ev = pair
        a = jev.call({"claim": secretscan.redact(claim), "evidence": secretscan.redact(ev)[:24_000]}, QUESTION,
                     deadline=deadline)["relation"]
        return {"relation": a["choice"], "confidence": a.get("confidence"), "probabilities": a.get("probabilities")}

    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(one, pairs))


# ---- model classifiers (one call per report) ------------------------------------------------------
MODEL_SYSTEM = ("You check whether cited evidence supports claims in an engineering report. You only read what you "
                "are given; you have no tools. Answer with JSON only.")
MODEL_PROMPT = """Each item below is one claim from an agent's report and the evidence it cites: file lines (numbered `N: text`, with some surrounding context) and/or the output of a command the agent ran.

For each item, decide how the evidence relates to the claim:
- "supports": the evidence states the claim or directly implies it. Reasonable paraphrase is fine; for a compound claim the core assertion must be backed.
- "contradicts": the evidence states the opposite or shows the claim is false (wrong number, wrong name, wrong behaviour).
- "says_nothing": the evidence doesn't contain what the claim says is there (wrong lines, unrelated code), so it can't back the claim.
Judge only against the evidence shown, not against what you believe in general. Check numbers exactly.

Reply with only a JSON array, one object per item, in order: [{"i": 1, "relation": "supports"}, ...]

"""
BACKENDS = ("jev", "haiku", "sonnet-low", "codex-low")
# evals/evidence_llm_eval.py, 128 hand-labelled claims from real reports: sonnet at low effort, flagging only
# "contradicts" (plus cited lines past EOF), P=0.86 R=0.50; Jev P=0.53 R=0.67; haiku and Codex-low below both.
DEFAULT_CLASSIFIER = "sonnet-low"


def _model_prompt(items):
    body = "\n\n".join(f"### Item {n}\nClaim: {secretscan.redact(c)}\nEvidence:\n{secretscan.redact(e)[:6000]}"
                        for n, (c, e) in enumerate(items, 1))
    return MODEL_PROMPT + body


def _parse_relations(text, n):
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        raise ValueError(f"no JSON array in reply: {(text or '')[:200]}")
    arr = json.loads(m.group(0))
    out = [None] * n
    for k, a in enumerate(arr):
        i = int(a.get("i", k + 1)) - 1
        if 0 <= i < n and a.get("relation") in ("supports", "contradicts", "says_nothing"):
            out[i] = {"relation": a["relation"], "confidence": 1.0}
    return out


def _claude(prompt, model, effort=None, timeout=45, system=MODEL_SYSTEM):
    """One tool-less `claude -p` turn (transitions.py uses it too) -> (reply text, cost meta)."""
    cmd = ["claude", "-p", "--model", model, "--tools", "", "--max-turns", "1", "--no-session-persistence",
           "--setting-sources", "", "--settings", '{"disableAllHooks": true}', "--strict-mcp-config",
           "--disable-slash-commands", "--system-prompt", system, "--output-format", "json"]
    if effort:
        cmd += ["--effort", effort]
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=str(config.STATE_DIR),
                       env=dict(os.environ, GUTCHECK_NESTED="1"))
    j = json.loads(r.stdout[:r.stdout.rindex("}") + 1])
    if j.get("is_error"):
        raise RuntimeError(str(j.get("result"))[:200])
    u = j.get("usage") or {}
    return j.get("result", ""), {"cost_usd": j.get("total_cost_usd"),
                                 "in_tokens": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                                 + (u.get("cache_creation_input_tokens") or 0), "out_tokens": u.get("output_tokens")}


def _codex(prompt, effort="low", timeout=300):
    from . import review
    comp = review.companion()
    if not comp:
        raise RuntimeError("Codex plugin companion script not found")
    work = config.STATE_DIR / "evidence-codex"
    work.mkdir(parents=True, exist_ok=True)
    pf = work / f"prompt-{os.getpid()}-{time.monotonic_ns()}.md"
    pf.write_text(prompt + "\n\nDo not run any commands or read any files; answer from the text above.")
    try:
        r = subprocess.run(["node", comp, "task", "--effort", effort, "--fresh", "--json", "--prompt-file", str(pf)],
                           capture_output=True, text=True, timeout=timeout, cwd=str(work))
    finally:
        pf.unlink(missing_ok=True)
    j = json.loads(r.stdout[r.stdout.index("{"):r.stdout.rindex("}") + 1])
    return j.get("rawOutput", ""), {"cost_usd": None}


def model_judge(items, backend):
    """items: [(claim, evidence)] -> ([{relation, confidence} | None], meta). One call for the whole report."""
    if not items:
        return [], {"seconds": 0}
    prompt = _model_prompt(items)
    t0 = time.monotonic()
    if backend == "haiku":
        text, meta = _claude(prompt, "haiku")
    elif backend == "sonnet-low":
        text, meta = _claude(prompt, "sonnet", "low")
    elif backend == "codex-low":
        text, meta = _codex(prompt, "low")
    else:
        raise ValueError(backend)
    meta["seconds"] = round(time.monotonic() - t0, 2)
    return _parse_relations(text, len(items)), meta


def check(report, cwd=None, transcript=None, as_of=None, deadline=None, max_claims=MAX_CLAIMS, backend=None):
    """Returns [{claim, cites, status, relation, confidence, numbers_missing, flag, reason}]."""
    backend = backend or config.load().get("evidence", {}).get("classifier", DEFAULT_CLASSIFIER)
    if backend != "jev":
        return _check_model(report, cwd, transcript, as_of, max_claims, backend)
    log = tool_log(transcript)
    cache, results, pairs = {}, [], []
    for cl in claims(report)[:max_claims]:
        evidence, problems, used = _gather(cl["cites"], cwd, cache, log, as_of, CONTEXT_LINES)
        if not evidence and not problems:
            continue
        nums = claim_numbers(cl["text"], cl["cites"]) if evidence else []
        missing = [n for n in nums if not number_in(n, evidence)]
        r = {"claim": cl["text"][:600], "cites": used, "problems": problems, "numbers_missing": missing,
             "evidence_chars": len(evidence)}
        results.append(r)
        if evidence:
            pairs.append((len(results) - 1, cl["text"], evidence))
    if pairs:
        try:
            verdicts = judge([(c, e) for _, c, e in pairs], deadline=deadline)
            for (i, _, _), v in zip(pairs, verdicts):
                results[i].update(v)
        except jev.JevError as e:
            for i, _, _ in pairs:
                results[i]["jev_error"] = str(e)[:200]
    # A "says nothing" verdict on a narrow window gets one wider look before it can become a flag.
    wide = [(i, c, e) for i, c, e in pairs if decide(results[i])[0] and not results[i]["problems"]
            and results[i].get("relation") != "contradicts"]
    if wide:
        rebuilt = []
        for i, claim, _ in wide:
            ev = _gather(claims(claim)[0]["cites"] if claims(claim) else [], cwd, cache, log, as_of, WIDE_CONTEXT)[0]
            if ev:
                rebuilt.append((i, claim, ev))
        try:
            for (i, _, ev), v in zip(rebuilt, judge([(c, e) for _, c, e in rebuilt], deadline=deadline)):
                results[i]["wide"] = v
                results[i]["evidence_chars"] = len(ev)
                results[i]["numbers_missing"] = [n for n in results[i]["numbers_missing"] if not number_in(n, ev)]
        except jev.JevError:
            pass
    for r in results:
        r["flag"], r["reason"] = decide(r)
    return results


def _check_model(report, cwd, transcript, as_of, max_claims, backend):
    """Same claims, citations and code checks as the Jev path; one model call per report on a wide window."""
    log = tool_log(transcript)
    cache, results, pairs = {}, [], []
    for cl in claims(report)[:max_claims]:
        evidence, problems, used = _gather(cl["cites"], cwd, cache, log, as_of, WIDE_CONTEXT)
        if not evidence and not problems:
            continue
        nums = claim_numbers(cl["text"], cl["cites"]) if evidence else []
        results.append({"claim": cl["text"][:600], "cites": used, "problems": problems,
                        "numbers_missing": [n for n in nums if not number_in(n, evidence)],
                        "evidence_chars": len(evidence)})
        if evidence:
            pairs.append((len(results) - 1, cl["text"], evidence))
    if pairs:
        try:
            verdicts, meta = model_judge([(c, e) for _, c, e in pairs], backend)
            for (i, _, _), v in zip(pairs, verdicts):
                if v:
                    results[i].update(v)
            for r in results:
                r["model_meta"] = meta
        except Exception as e:  # fail open: code checks still apply
            for i, _, _ in pairs:
                results[i]["model_error"] = f"{type(e).__name__}: {str(e)[:150]}"
    for r in results:
        r["flag"], r["reason"] = decide(r)
    return results


def decide(r):
    """Flag only what is clearly unsupported (precision first: a false flag costs the subagent a turn)."""
    if r["problems"]:
        return True, r["problems"][0]
    if r.get("model_meta") is not None or r.get("model_error"):
        # model classifiers: "contradicts" only; "says nothing" and missing numbers are logged, not flagged
        # (both lowered precision below 0.8 in the eval)
        return (True, "contradicted by its citation") if r.get("relation") == "contradicts" else (False, None)
    rel, conf = r.get("relation"), r.get("confidence") or 0
    if r.get("wide"):
        rel, conf = r["wide"]["relation"], r["wide"].get("confidence") or 0
    if rel in ("contradicts", "says_nothing") and conf >= AUTO_ACCEPT:
        return True, "contradicted by its citation" if rel == "contradicts" else "its citation doesn't show this"
    if r["numbers_missing"] and rel != "supports":
        return True, f"number {r['numbers_missing'][0]} isn't in the cited evidence"
    return False, None


def feedback(results, limit=2):
    """One short line for the subagent, or None."""
    bad = [r for r in results if r["flag"]]
    if not bad:
        return None
    items = "; ".join(f"{_cite_short(r)} (\"{_short(r['claim'], 50)}\": {r['reason']})" for r in bad[:limit])
    more = f" +{len(bad) - limit} more" if len(bad) > limit else ""
    return f"tokenmaxxing: {len(bad)} cited claim(s) may not match their citations: {items}{more}. Re-check; correct or confirm in one line."


def _cite_short(r):
    c = r["cites"][0] if r["cites"] else (r["problems"][0].split(": ")[0] if r["problems"] else "citation")
    return c.rsplit("/", 1)[-1]


def _short(s, n=90):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


# ---- hook --------------------------------------------------------------------------------------
def last_assistant_text(transcript):
    last = None
    try:
        with open(transcript, errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                c = (d.get("message") or {}).get("content")
                if d.get("type") == "assistant" and isinstance(c, list):
                    t = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
                    if t.strip():
                        last = t
    except OSError:
        return None
    return last


def on_subagent_stop(inp):
    """SubagentStop: at most one line of feedback to the subagent about unsupported claims; never blocks.
    Returns hook output dict or None."""
    cfg = config.load().get("evidence", {})
    mode = cfg.get("mode", "on")  # sonnet-low classifier cleared the 0.8 precision bar (see DEFAULT_CLASSIFIER)
    if mode == "off" or inp.get("stop_hook_active"):
        return None
    report = inp.get("last_assistant_message") or last_assistant_text(inp.get("agent_transcript_path") or "")
    if not report or not CITE_RE.search(report) and "`" not in report:
        return None
    t0 = time.monotonic()
    results = check(report, cwd=inp.get("cwd"), transcript=inp.get("agent_transcript_path"),
                    deadline=float(cfg.get("deadline_s", 12)))
    msg = feedback(results)
    _log({"session": inp.get("session_id"), "agent_id": inp.get("agent_id"), "agent_type": inp.get("agent_type"),
          "mode": mode, "claims": len(results), "flagged": sum(r["flag"] for r in results),
          "flags": [{"claim": r["claim"][:200], "reason": r["reason"]} for r in results if r["flag"]][:10],
          "ms": int((time.monotonic() - t0) * 1000)})
    if msg and mode == "on":
        return {"hookSpecificOutput": {"hookEventName": "SubagentStop", "additionalContext": msg}}
    return None


def _log(rec):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")
    except OSError:
        pass


def main(args):
    from gutcheck_core.args import _flag, _opt
    cwd = _opt(args, "--cwd", os.getcwd())
    transcript = _opt(args, "--transcript")
    show_all = _flag(args, "--all")
    src = args[0] if args else "-"
    report = sys.stdin.read() if src == "-" else open(src, errors="replace").read()
    results = check(report, cwd=cwd, transcript=transcript)
    for r in results:
        if show_all or r["flag"]:
            tag = "UNSUPPORTED" if r["flag"] else "ok"
            print(f"[{tag}] {_short(r['claim'], 160)}\n    cites {', '.join(r['cites']) or '-'}; "
                  f"jev={r.get('relation')}@{(r.get('confidence') or 0):.2f}"
                  f"{'; missing numbers ' + ','.join(r['numbers_missing']) if r['numbers_missing'] else ''}"
                  f"{'; ' + r['reason'] if r['reason'] else ''}")
    print(f"# {sum(r['flag'] for r in results)}/{len(results)} cited claims flagged", file=sys.stderr)


def hook(inp):
    """HANDLERS entry for SubagentStop: prints the hook output, if any."""
    out = on_subagent_stop(inp)
    if out:
        sys.stdout.write(json.dumps(out))
