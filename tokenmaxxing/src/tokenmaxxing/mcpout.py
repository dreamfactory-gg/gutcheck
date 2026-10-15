"""Core MCP output layer (any MCP server): large outputs are saved in full and Claude gets a compact view
plus the path, so it reads exact ranges locally. Lossless; judged on net tokens and task success.

View by content type:
  JSON  -> structural skeleton (keys, types, array lengths, one sample item) with line refs into the saved file
  code  -> tree-sitter outline via ast-outline (signatures + line ranges)
  other -> head + tail
Adapters (tokenmaxxing/adapters/<mcp>/) can take over specific tools first. Servers listed in
`mcp_trim.pass_through` (default: Roblox_Studio) are never touched: their output reaches Claude as it is.
"""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from gutcheck_core import config


def out_dir():
    d = Path(config.plugin_data() or config.STATE_DIR) / "mcp-out"
    d.mkdir(parents=True, exist_ok=True)
    return d


def passes_through(server, cfg=None):
    """Is this MCP server's output left untouched (no view, no saved file)? Roblox_Studio by default: Studio
    calls go direct and a Studio MCP proxy, when installed, shapes their output at the source."""
    cfg = cfg if cfg is not None else config.load().get("mcp_trim", {})
    return server in (cfg.get("pass_through") or [])


def text_of(resp):
    if isinstance(resp, str):
        return resp
    if isinstance(resp, list):
        return "\n".join(b.get("text", "") for b in resp if isinstance(b, dict) and b.get("type", "text") == "text")
    if isinstance(resp, dict) and isinstance(resp.get("content"), list):
        return "\n".join(b.get("text", "") for b in resp["content"] if isinstance(b, dict))
    return json.dumps(resp) if resp is not None else ""


def has_images(resp):
    items = resp if isinstance(resp, list) else (resp or {}).get("content") if isinstance(resp, dict) else []
    return any(isinstance(b, dict) and b.get("type") == "image" for b in items or [])


def save(tool, text, ext=".txt"):
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", tool)[:60]
    p = out_dir() / f"{name}-{hashlib.sha1(text.encode()).hexdigest()[:10]}{ext}"
    p.write_text(text)
    return p


# ---- JSON skeleton ------------------------------------------------------------------------------
def _skel(v, depth, max_depth, max_keys=25):
    pad = "  " * depth
    if isinstance(v, dict):
        if depth >= max_depth:
            return f"{{…{len(v)} keys}}"
        lines = ["{"]
        for i, (k, x) in enumerate(v.items()):
            if i >= max_keys:
                lines.append(f"{pad}  … {len(v) - max_keys} more keys")
                break
            lines.append(f"{pad}  {json.dumps(k)}: {_skel(x, depth + 1, max_depth)}")
        return "\n".join(lines + [pad + "}"])
    if isinstance(v, list):
        if not v:
            return "[]"
        if depth >= max_depth:
            return f"[…{len(v)} items]"
        return f"[{len(v)} items; first: {_skel(v[0], depth + 1, max_depth)}]"
    if isinstance(v, str):
        return json.dumps(v if len(v) <= 60 else v[:57] + "…")
    return json.dumps(v)


def json_view(text):
    try:
        v = json.loads(text)
    except ValueError:
        return None
    if not isinstance(v, (dict, list)):
        return None
    return _skel(v, 0, 4)


# ---- code outline -----------------------------------------------------------------------------------
_LANG_HINTS = [
    (".py", re.compile(r"^\s*(def |class |import |from \S+ import )", re.M)),
    (".rs", re.compile(r"^\s*(pub )?(fn |struct |impl |enum |mod |use )", re.M)),
    (".go", re.compile(r"^\s*(func |package |type \w+ struct)", re.M)),
    (".ts", re.compile(r"^\s*(export |interface |const \w+\s*[:=]|function |class )", re.M)),
    (".lua", re.compile(r"^\s*(local function |function [\w.:]+\(|local \w+ = require\()", re.M)),
]


def code_ext(text):
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 30:
        return None
    best, score = None, 0
    for ext, rx in _LANG_HINTS:
        n = len(rx.findall(text))
        if n > score:
            best, score = ext, n
    return best if score >= 5 else None


def code_view(text, ext):
    tool = shutil.which("ast-outline")
    if not tool:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=ext, delete=False) as fh:
        fh.write(text)
    try:
        out = subprocess.run([tool, fh.name], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    finally:
        os.unlink(fh.name)
    return out.replace(fh.name, "<saved file>") if out.strip() else None


def head_tail(text, head=2500, tail=1500):
    return text[:head], text[-tail:]


# ---- kinds and levels ---------------------------------------------------------------------------------
# Every MCP tool call is one of three kinds (adapter hint for the server, else learned from history, else the name):
#   action: edits, inserts, play start/stop -> a receipt ("✓ ok · saved <path>"), or the error lines on failure
#   log:    console output, logs            -> only errors, warnings and unexpected lines, plus the path
#   read:   reads, searches, tree dumps     -> an outline (or the head and tail) plus the path; never a bare receipt
# The level is how tight the view is. Waste tracking (mcpwaste.py) moves a tool along this ladder over time.
LEVELS = ("pass", "headtail", "outline", "receipt")
MAX_LEVEL = {"read": "outline", "log": "receipt", "action": "receipt"}
DEFAULT_LEVEL = {"read": "headtail", "log": "receipt", "action": "receipt"}
MIN_CHARS = {"headtail": 8000, "outline": 3000, "receipt": 600}  # smaller outputs pass through

_ACTION_NAME = re.compile(r"(?:^|_)(edit|write|insert|create|delete|remove|update|set|start|stop|play|move|click|"
                          r"upload|generate|apply|install|close|navigate|commit|push|send|post|put|patch|rename|"
                          r"save|batch|resize|select|switch|drag|type|press|scroll)(?:_|$)")
_LOG_NAME = re.compile(r"(?:^|_)(console|logs?|output|messages)(?:_|$)")

FAIL_RE = re.compile(
    r"(?i)(\berr(or)?s?\b|\bfail(ed|ure|ures|s|ing)?\b|exception|traceback|\bpanic|\bfatal\b|\bcrash(ed)?\b|"
    r"\babort(ed)?\b|\bdenied\b|reject(ed|ion|s)?\b|\bmissing\b|forbidden|unauthori[sz]ed|timed? ?out\b|\binvalid\b|not found|no such|cannot|"
    r"can't|couldn't|could not|unable to|refused|mismatch|unexpected|assert|attempt to (index|call|perform|compare|"
    r"concatenate|get)|infinite yield|stack begin|is not a valid member|\bexpected\b.*\bgot\b|\bnil\b.*\bvalue\b|"
    r"^\s*(?:expected|received):|✗|❌|\bFAIL\b|\bERR\b)")
WARN_RE = re.compile(r"(?i)(\bwarn(ing|ings)?\b|⚠)")
_STACK_RE = re.compile(r"^\s*(Script '|Stack (Begin|End)|  ?Line \d+|\s+at |\s+File \")")
_ASSERT_DIFF = re.compile(r"^\s*- Expected\b")  # jest's "- Expected  - 1"; its -/+ lines below hold the values
_DIFF_LINE = re.compile(r"^\s*[-+]\s")


def name_kind(tool):
    """Kind from the tool's name alone (mcp__server__tool or bare tool)."""
    t = tool.split("__")[-1].lower()
    if _LOG_NAME.search(t):
        return "log"
    if _ACTION_NAME.search(t):
        return "action"
    return "read"


_ZERO = re.compile(r'^\w*"?\s*(?:[:=]\s*(?:0\b|false\b|nil\b|none\b|null\b|\[\]|\{\})|\s+0\b)', re.I)


def _real_failure(line, m):
    """A match that isn't a zero count ("err":0, failures=0, errors: 0, 0 errors)."""
    if m.group().strip().lower() in ("expected:", "received:"):
        return True  # zero is assertion detail here, not a zero failure count
    if _ZERO.match(line[m.end():m.end() + 12]):
        return False
    return not re.search(r"\b(0|no|zero)\s+\W?$", line[max(0, m.start() - 6):m.start()], re.I)


def _is_failure(line):
    return any(_real_failure(line, m) for m in FAIL_RE.finditer(line)) and not WARN_RE.search(line)


def _snips(line, width=160):
    """For a very long line (one-line JSON, minified output): the stretches around each real failure match."""
    spans = []
    for m in FAIL_RE.finditer(line):
        if _real_failure(line, m):
            a, b = max(0, m.start() - width), min(len(line), m.end() + width)
            if spans and a <= spans[-1][1]:
                spans[-1] = (spans[-1][0], b)
            else:
                spans.append((a, b))
    return " … ".join(line[a:b] for a, b in spans[:6])


def failure_lines(text, limit=40, stack=3, long_line=400):
    """(line number, line) for error lines, each with up to `stack` following stack-trace lines. Zero counts
    ("errors=0") don't count; a very long line is cut to the stretches around its failures."""
    lines = text.splitlines()
    out, seen = [], set()
    for i, ln in enumerate(lines):
        diff = _ASSERT_DIFF.match(ln)
        if diff or _is_failure(ln):
            for j in range(i, min(len(lines), i + 1 + (12 if diff else stack))):
                if j == i or _STACK_RE.match(lines[j]) or diff and _DIFF_LINE.match(lines[j]):
                    if j not in seen:
                        seen.add(j)
                        out.append((j + 1, _snips(lines[j], width=long_line * 2 // 5) if len(lines[j]) > long_line else lines[j]))
                elif j > i and not diff:
                    break
        if len(out) >= limit:
            break
    return out


def warning_lines(text, limit=40):
    return [(i + 1, ln) for i, ln in enumerate(text.splitlines()) if WARN_RE.search(ln)][:limit]


def norm_line(line):
    """Line with numbers, hex ids and quoted values blanked, for routine-line counting."""
    line = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "#", line.strip().lower())
    line = re.sub(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", "#", line)
    return re.sub(r"\d+(\.\d+)?", "#", line)[:200]


def line_key(line):
    return hashlib.sha1(norm_line(line).encode()).hexdigest()[:12]


def _numbered(items, width=0):
    return "\n".join(f"{n:>{width}}: {ln if len(ln) <= 1200 else ln[:1200] + ' …'}" for n, ln in items)


def _dedupe(items, normalize=True):
    """Collapse lines that are identical after normalising numbers: keep the first, count the rest."""
    first, count = {}, {}
    for n, ln in items:
        k = norm_line(ln) if normalize else ln.strip()
        if k not in first:
            first[k] = (n, ln)
        count[k] = count.get(k, 0) + 1
    return [(n, ln + (f"  (×{count[k]})" if count[k] > 1 else "")) for k, (n, ln) in first.items()]


def render(tool, text, kind, level, cfg, save_fn=None, routine=None, judge=None, purpose="", command="", brief="",
           description="", *, compact=False):
    """Compact view of `text` at `level` for a tool of `kind`, or None to pass it through.
    save_fn(ext) -> saved path (None for a dry run: learning and evals); routine: set of line keys seen before in
    this tool's output (log kind); judge(lines) -> subset of lines that show a failure or something unexpected.
    compact: Bash output, cut to the lines its purpose (agent text, user prompt, brief, description) asks for."""
    if level == "pass" or not text:
        return None
    kind = kind if kind in MAX_LEVEL else "read"
    if LEVELS.index(level) > LEVELS.index(MAX_LEVEL[kind]):
        level = MAX_LEVEL[kind]
    if len(text) <= cfg.get(f"min_chars_{level}", MIN_CHARS[level] if level != "headtail" else cfg.get("min_chars", 8000)):
        return None
    save_fn = save_fn or (lambda ext: "<saved file>")
    nlines = text.count("\n") + 1
    if level == "receipt" and kind == "action":
        path = save_fn(".txt")
        fails = failure_lines(text, limit=30) + [x for x in warning_lines(text, 10)]
        verdict = _json_failure(text)
        first = text.strip().splitlines()[0][:160] if text.strip() else ""
        if fails or verdict:
            return (f"✗ failed or warned · {len(text):,} chars, full output saved to {path}\n"
                    + _numbered(verdict + sorted(_dedupe(fails))))
        return f"✓ ok · {first} · full output ({len(text):,} chars) saved to {path}"
    if level == "receipt" and kind == "log":
        return _log_view(text, nlines, save_fn, routine or set(), judge, cfg)
    if level == "outline":
        return _outline_view(tool, text, nlines, save_fn, cfg)
    if not compact:
        return _headtail_view(text, nlines, save_fn, cfg)
    try:
        return _bash_view(text, nlines, save_fn, cfg, purpose, command, brief, description)
    except Exception:
        return _bash_view(text, nlines, save_fn, cfg)  # purpose selection must never prevent the ordinary trim


_JSON_START = re.compile(r"^[ \t\r]*[\[{]", re.M)
_WS = re.compile(r"\s*")


def _json_failure(text):
    """(key, value) for the top-level keys that mark a JSON object reply failed (success/ok false, an error or a
    reason), in each run of JSON documents that starts a line: text_of joins an MCP reply's blocks with newlines."""
    dec, out, end = json.JSONDecoder(), [], 0
    for m in _JSON_START.finditer(text):
        i = m.end() - 1
        while i >= end and i < len(text):
            try:
                d, end = dec.raw_decode(text, i)
            except ValueError:
                break
            if isinstance(d, dict):
                out += [(k, json.dumps(d[k])[:300]) for k in ("success", "ok", "error", "reason")
                        if k in d and (d[k] is False if k in ("success", "ok") else d[k])]
            i = _WS.match(text, end).end()
    return out


def _fail_block(text, limit=20):
    f = failure_lines(text, limit)
    return ("\n[tokenmaxxing: error lines (line: text)]\n" + _numbered(f)) if f else ""


def _middle_failures(text, head, tail, limit=20):
    """Error lines in the part of the output a head+tail view drops, so no failure is hidden."""
    lo, hi = len(head), len(text) - len(tail)
    start = text.count("\n", 0, lo)
    mid = text[lo:hi]
    return [(n + start, ln) for n, ln in failure_lines(mid, limit)]


# Stands in for the saved path while a view is sized, so the file is written only when the view is used.
_SAVED = "<saved file>".ljust(150)


def _omitted(text, h, t, nlines, path, extra):
    """Head, the omitted-part marker naming the saved file, `extra`, tail."""
    return (h + f"\n\n… [tokenmaxxing: {len(text) - len(h) - len(t):,} chars omitted; full output ({len(text):,} chars, "
            f"{nlines:,} lines) saved to {path}; Read or grep it] …\n{extra}\n" + t)


_PURPOSE_WORDS = re.compile(r"\b\w{3,}\b")
_PURPOSE_STOP = set("the and for that this with from into what which when where how are was were has have "
                    "had will would could should not but you your its our their they them then than only "
                    "please tell show read run output command file lines reply nothing else".split())


def _purpose_lines(text, purpose, command, ties="", max_chars=3000):
    """Numbered lines matching the purpose's words (with a line either side); `ties` words only rank lines."""
    def words(s):
        return set(_PURPOSE_WORDS.findall(s.lower()))

    terms = words(purpose) - words(command) - _PURPOSE_STOP
    ties = words(ties) - words(command) - _PURPOSE_STOP - terms
    if not terms:
        return ""
    lines = text.splitlines()
    hits = [words(ln) & (terms | ties) for ln in lines]
    counts = Counter(term for hit in hits for term in hit)
    weights = {term: math.log(len(lines) / count) for term, count in counts.items()}
    scores = [(sum(weights[term] for term in hit & terms), sum(weights[term] for term in hit & ties)) for hit in hits]
    kept = {}
    for i in sorted(range(len(lines)), key=lambda i: (-scores[i][0], -scores[i][1], i)):
        if scores[i][0] <= 0:
            break
        group = {j + 1: lines[j] for j in range(max(0, i - 1), min(len(lines), i + 2))}
        candidate = kept | group
        if len(candidate) <= 40 and len(_numbered(sorted(candidate.items()))) <= max_chars:
            kept = candidate
    return _numbered(sorted(kept.items()))


def _headtail_view(text, nlines, save_fn, cfg):
    jv = json_view(text)
    if jv is not None:
        p = save_fn(".json")
        return (f"[tokenmaxxing: {len(text):,}-char JSON output saved to {p}; structure below. Read or `jq` the file "
                f"for the values you need.]\n{jv}" + _fail_block(text))
    ext = code_ext(text)
    cv = code_view(text, ext) if ext else None
    if cv:
        p = save_fn(ext)
        return (f"[tokenmaxxing: {len(text):,}-char code output saved to {p}; outline below (line ranges refer to "
                f"the saved file). Read the ranges you need.]\n{cv}")
    p = save_fn(".txt")
    h, t = head_tail(text, cfg.get("head_chars", 2500), cfg.get("tail_chars", 1500))
    mid = _middle_failures(text, h, t)
    extra = ("\n[tokenmaxxing: error lines in the omitted part]\n" + _numbered(mid) + "\n") if mid else ""
    return _omitted(text, h, t, nlines, p, extra)


def _error_block(title, items, cap=20):
    """Up to `cap` error lines under `title`, and how many more the saved file holds."""
    more = len(items) - cap
    return (f"\n[tokenmaxxing: {title}]\n" + _numbered(items[:cap])
            + (f"\n… {more} more error lines in the saved file" if more > 0 else "") + "\n") if items else ""


def _bash_view(text, nlines, save_fn, cfg, purpose="", command="", brief="", description=""):
    """Bash output: the lines its purpose asks for and its error lines, in at most half the output (2.5k chars
    under 8k, 3k above while the purpose lines fit), or None to pass it through. Under 8k, a diff, JSON, or output
    no purpose word from the agent or the user matches passes through: a view would only lose its meaning."""
    small = len(text) < 8000
    if small:
        if re.search(r"^(?:diff --git |@@)", text, re.M):
            return None
        try:
            json.loads(text)
            return None
        except ValueError:
            pass
    cap = min(len(text) // 2, 2500 if small else 3000)
    fails = _dedupe(failure_lines(text, limit=nlines, long_line=200), normalize=False)
    budget = max(0, cap - len(_error_block("", fails)) - 300)
    terms, ties = (purpose, f"{brief} {description}") if small else (f"{description} {purpose}", brief)
    matches = _purpose_lines(text, terms, command, ties, min(1200, budget))
    if small and not matches:
        return None
    block = ("\n[tokenmaxxing: lines matching the call's purpose (line: text)]\n" + matches + "\n") if matches else ""
    jv = json_view(text) if not block else None
    if jv is not None:
        ext = ".json"

        def make(p):
            return (f"[tokenmaxxing: {len(text):,}-char JSON output saved to {p}; structure below. Read or `jq` the file "
                    f"for the values you need.]\n{jv}" + _error_block("error lines (line: text)", fails))
    else:
        ext = ".txt"
        lines = text.splitlines(keepends=True)
        head = min(len("".join(lines[:10])), max(0, budget - len(block)) * 5 // 8)
        tail = min(len("".join(lines[-10:])), max(0, budget - len(block)) * 3 // 8)

        def make(p):
            h, t = head_tail(text, head, tail)
            t = t if tail else ""
            lo, hi = text.count("\n", 0, len(h)) + 1, text.count("\n", 0, len(text) - len(t)) + 1
            mid = [(n, ln) for n, ln in fails if lo <= n <= hi]
            return _omitted(text, h, t, nlines, p, _error_block("error lines in the omitted part", mid) + block)

        if not small and (not block or len(make(_SAVED)) > cap):
            block = ""
            room = max(0, len(text) // 2 - len(_error_block("", fails)) - 300 - len(_SAVED))
            head, tail = min(cfg.get("head_chars", 2500), room * 5 // 8), min(cfg.get("tail_chars", 1500), room * 3 // 8)
    if len(make(_SAVED)) > (cap if small else len(text) // 2):
        return None
    return make(save_fn(ext))


def _outline_view(tool, text, nlines, save_fn, cfg):
    try:
        v = json.loads(text)
    except ValueError:
        v = None
    if isinstance(v, (dict, list)):
        p = save_fn(".json")
        return (f"[tokenmaxxing: {len(text):,}-char JSON output saved to {p}; top-level structure below. Read or `jq` "
                f"the file for values.]\n{_skel(v, 0, 2, max_keys=15)}" + _fail_block(text))
    ext = code_ext(text)
    cv = code_view(text, ext) if ext else None
    if cv:
        p = save_fn(ext)
        return (f"[tokenmaxxing: {len(text):,}-char code output saved to {p}; outline below (line ranges refer to "
                f"the saved file). Read the ranges you need.]\n{cv}")
    p = save_fn(".txt")
    h, t = head_tail(text, cfg.get("outline_head_chars", 1000), cfg.get("outline_tail_chars", 500))
    mid = _middle_failures(text, h, t)
    extra = ("\n[tokenmaxxing: error lines in the omitted part]\n" + _numbered(mid) + "\n") if mid else ""
    return _omitted(text, h, t, nlines, p, extra)


def _log_view(text, nlines, save_fn, routine, judge, cfg):
    """Errors and warnings always (deduplicated); other lines only when unexpected: never seen in this tool's
    earlier output, then (if a judge is given) confirmed by it. Routine repeated warnings are counted, not shown."""
    lines = text.splitlines()
    fails = failure_lines(text, limit=80)
    fail_nums = {n for n, _ in fails}
    warns = [(n, ln) for n, ln in warning_lines(text, 400) if n not in fail_nums]
    shown_nums = fail_nums | {n for n, _ in warns}
    fresh, seen_keys = [], set()
    for i, ln in enumerate(lines, 1):
        if i in shown_nums or not ln.strip():
            continue
        k = line_key(ln)
        if k in routine or k in seen_keys:
            continue
        seen_keys.add(k)
        fresh.append((i, ln))
    unexpected = fresh
    if judge and fresh:
        try:
            unexpected = judge(fresh)
        except Exception:
            unexpected = fresh  # judge unavailable: show every unseen line (fail open, show more)
    cap = cfg.get("log_max_lines", 60)
    body = sorted(_dedupe(fails) + _dedupe(warns) + unexpected[:cap])
    more = len(unexpected) - cap if len(unexpected) > cap else 0
    if not body:
        return f"✓ no errors, warnings or unexpected lines in {nlines:,} lines · full output saved to {save_fn('.log')}"

    def make(p):
        return (f"[tokenmaxxing: {nlines:,}-line log; {len(fails)} error, {len(warns)} warning, {len(unexpected)} "
                f"unexpected line(s); full output saved to {p} (numbers below are its line numbers)"
                + (f"; {more} more unexpected lines in the file" if more else "") + "]\n" + _numbered(body))

    return make(save_fn(".log")) if len(make(_SAVED)) < len(text) * 0.9 else None


def view(tool, text, cfg):
    """Return (compact view, saved path) or (None, None) when the output should pass through (read kind,
    head/tail level: the original behaviour)."""
    saved = {}

    def save_fn(ext):
        saved["p"] = save(tool, text, ext)
        return saved["p"]
    out = render(tool, text, "read", "headtail", cfg, save_fn)
    return (out, saved.get("p")) if out else (None, None)


# ---- per-tool policy (written by mcpwaste.track at refresh) ------------------------------------------
POLICY = "mcp_policy.json"
_policy = None


def policy():
    global _policy
    if _policy is None:
        try:
            _policy = json.loads((config.STATE_DIR / POLICY).read_text())
        except (OSError, ValueError):
            _policy = {}
    return _policy


def policy_key(tool, kind, per_call):
    """Mixed tools (kind decided per call by an adapter) keep one policy per kind."""
    return f"{tool}#{kind}" if per_call else tool


def kind_for(tool, tool_input, text, adapter=None):
    """(kind, per_call): adapter hint first, then the kind learned from history, then the tool's name."""
    short = tool.split("__", 2)[-1]
    if adapter is not None and hasattr(adapter, "kind"):
        k = adapter.kind(short, tool_input or {}, text)
        if k:
            return k, short in getattr(adapter, "PER_CALL_KINDS", ())
    k = (policy().get(tool) or {}).get("kind")
    return (k or name_kind(tool)), False


def level_for(key, kind):
    p = policy().get(key) or {}
    lv = p.get("level")
    if lv in LEVELS:
        return lv
    if p.get("mode") == "pass":  # policy written by the earlier learner
        return "pass"
    return DEFAULT_LEVEL.get(kind, "headtail")


def mode_for(tool):
    """Back-compat: 'pass' or 'trim'."""
    return "pass" if level_for(tool, name_kind(tool)) == "pass" else "trim"


def learn(**_):
    """Rebuild the per-tool policy from all history (bootstrap). See mcpwaste.track."""
    from . import mcpwaste
    return mcpwaste.track(full=True)


# ---- routine lines (log kind): which lines this tool prints on every run -------------------------------
ROUTINE = "mcp_routine.json"


def routine_for(tool):
    try:
        return set((json.loads((config.STATE_DIR / ROUTINE).read_text()).get(tool) or {}).keys())
    except (OSError, ValueError):
        return set()


def routine_update(tool, text, min_seen=2, cap=4000):
    """Count this output's distinct lines; a line counts as routine once seen in `min_seen` outputs."""
    p = config.STATE_DIR / ROUTINE
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        d = {}
    counts = d.setdefault(tool + "#counts", {})
    for k in {line_key(l) for l in text.splitlines() if l.strip()}:
        counts[k] = counts.get(k, 0) + 1
    if len(counts) > cap * 2:
        counts = dict(sorted(counts.items(), key=lambda kv: -kv[1])[:cap])
        d[tool + "#counts"] = counts
    d[tool] = {k: 1 for k, n in counts.items() if n >= min_seen}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(p)
