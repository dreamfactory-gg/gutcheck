"""Detect and redact secret-shaped strings; recognise secret files and secret-printing commands."""
import math
import re

# Prefixed tokens: provider-specific shapes. Kept conservative so prose never matches.
PREFIXED = [
    ("anthropic", r"(?<![\w-])sk-ant-[A-Za-z0-9_\-]{20,}"),
    ("openai", r"(?<![\w-])sk-(?=[\w-]*\d)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"),
    ("typesafe", r"apik[_\-][A-Za-z0-9_\-]{16,}"),
    ("vercel", r"vck_[A-Za-z0-9_\-]{16,}"),
    ("github", r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"),
    ("aws-access-key", r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    ("slack", r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    ("stripe", r"(?<![\w-])(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("google", r"AIza[0-9A-Za-z_\-]{35}"),
    ("private-key", r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    ("jwt", r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ("hf", r"hf_[A-Za-z0-9]{30,}"),
    ("discord", r"[MN][A-Za-z\d]{23,25}\.[\w-]{6}\.[\w-]{27,38}"),
    ("roblox-cookie", r"_\|WARNING:-DO-NOT-SHARE-THIS[^\s\"']{20,}"),
]
_PREFIXED_RE = re.compile("|".join(f"(?P<g{i}>{p})" for i, (_, p) in enumerate(PREFIXED)))

# assignment of a long value to a secret-named variable: API_KEY=..., "token": "..."
_ASSIGN_RE = re.compile(
    r"""(?i)\b[\w.\-]*(?:api[_\-]?key|secret|token|passw(?:or)?d|credential|auth)[\w.\-]*["']?\s*[:=]\s*["']?"""
    r"""(?P<v>[A-Za-z0-9_\-+/=.]{16,})""")
_LONG_RE = re.compile(r"(?<![A-Za-z0-9_\-+/=.])[A-Za-z0-9_\-+/]{32,}={0,2}(?![A-Za-z0-9_\-+/=.])")
_HEXISH = re.compile(r"^[0-9a-f]+$")


def entropy(s):
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def _looks_random(tok):
    """A long high-entropy token that isn't a hash, path, uuid or ordinary identifier."""
    if len(tok) < 32 or "/" in tok and tok.count("/") > 2:
        return False
    if _HEXISH.match(tok.lower()) and len(tok) in (32, 40, 56, 64, 96, 128):
        return False  # md5/sha digests and git SHAs are everywhere in coding sessions
    classes = sum(bool(re.search(p, tok)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    return classes >= 3 and entropy(tok) >= 4.2


def find(text, high_entropy=True):
    """Return [(kind, start, end)] of secret-shaped spans."""
    out = []
    for m in _PREFIXED_RE.finditer(text):
        i = int(m.lastgroup[1:])
        out.append((PREFIXED[i][0], m.start(), m.end()))
    for m in _ASSIGN_RE.finditer(text):
        v = m.group("v")
        if entropy(v) >= 3.5 and re.search(r"\d", v) and re.search(r"[A-Za-z]", v):
            out.append(("assigned-secret", m.start("v"), m.end("v")))
    if high_entropy:
        for m in _LONG_RE.finditer(text):
            if _looks_random(m.group()):
                out.append(("high-entropy", m.start(), m.end()))
    out.sort(key=lambda x: (x[1], -x[2]))
    merged = []
    for k, s, e in out:
        if merged and s < merged[-1][2]:
            continue
        merged.append((k, s, e))
    return merged


def redact(text, known_only=False):
    """Spans become <KIND-REDACTED>; known_only: provider key formats only (no assignments, no high-entropy)."""
    spans = [x for x in find(text, not known_only) if not known_only or x[0] in BLOCK_KINDS]
    for k, s, e in reversed(spans):
        text = text[:s] + f"<{k.upper()}-REDACTED>" + text[e:]
    return text


# ---- secret files -------------------------------------------------------------------------
SAFE_ENV = re.compile(r"\.env(\.[\w\-]+)*\.(example|sample|template|dist|defaults?)$", re.I)
_SECRET_FILE = re.compile(
    r"(^|/)(\.env(\.[\w.\-]+)?|[^/]*\.pem|[^/]*\.key|[^/]*\.p12|[^/]*\.pfx|id_(rsa|ed25519|ecdsa|dsa)"
    r"|\.netrc|\.pgpass|\.pypirc|\.npmrc|credentials(\.json)?|\.credentials\.json|secrets?\.(json|ya?ml|toml)"
    r"|service[-_]account[^/]*\.json|\.git-credentials|\.dockercfg|config\.json(?=$)(?<=docker/config\.json))$")


def is_secret_file(path):
    p = path.strip("'\"")
    if not p or p.startswith("-"):
        return False
    if SAFE_ENV.search(p):
        return False
    if p.endswith((".pub", ".example")):
        return False
    return bool(_SECRET_FILE.search(p)) or "/.aws/credentials" in p or p.endswith("/.aws/credentials")


SECRET_VAR = re.compile(r"(?i)(key|token|secret|passw|pwd|cred|auth|cookie|session)")
_NOT_SECRET_VARS = {"PWD", "OLDPWD", "SSH_AUTH_SOCK", "XAUTHORITY", "KEYCHAIN", "SESSION_ID", "TERM_SESSION_ID",
                    "KEYTIMEOUT", "HISTFILE", "TOKENIZERS_PARALLELISM", "CLAUDE_CODE_SESSION_ID"}


def is_secret_var(name):
    u = name.upper()
    if u in _NOT_SECRET_VARS or u.endswith(("_PATH", "_FILE", "_DIR", "_URL", "_HOST", "_ID", "_NAME", "_SOCK",
                                            "_TIMEOUT", "_LEN", "_COUNT")):
        return False
    return bool(re.search(r"(KEY|TOKEN|SECRET|PASSW|PWD|CRED|COOKIE|AUTH)", u))


# ---- prompt policy: block only on known key formats -----------------------------------------------
_URL = re.compile(r"(?i)\b(?:https?|wss?|ftp)://[^\s<>\"'`)\]]+")
BLOCK_KINDS = {k for k, _ in PREFIXED}
# docs and templates: AKIA...EXAMPLE, sk-your-key-here, ghp_xxxxxxxx
_PLACEHOLDER = re.compile(r"(?i)example|your|placeholder|dummy|redacted|changeme|x{6,}|\*{3,}")


def _url_spans(text):
    return [(m.start(), m.end(), m.group()) for m in _URL.finditer(text)]


def _in_query_value(url, rel_start):
    """True when the position inside `url` falls in a query-string value (after '?' and '=')."""
    q = url.find("?")
    if q < 0 or rel_start <= q:
        return False
    return url.rfind("=", q, rel_start + 1) > max(url.rfind("&", q, rel_start + 1), q)


def classify_prompt(text, skip_placeholders=False):
    """(block_kinds, warn_kinds) for a prompt. Known key formats block; anything else only warns.
    Inside URLs nothing counts except a known key format sitting in a query value (?api_key=sk-...).
    skip_placeholders (tool inputs): example and template values don't count."""
    urls = _url_spans(text)
    block, warn = set(), set()
    for kind, s, e in find(text):
        if skip_placeholders and (_PLACEHOLDER.search(text[s:e]) or entropy(text[s:e]) < 3.0):
            continue
        url = next(((us, ue, u) for us, ue, u in urls if us <= s < ue), None)
        known = kind in BLOCK_KINDS
        if url:
            if known and _in_query_value(url[2], s - url[0]):
                block.add(kind)
            continue
        (block if known else warn).add(kind)
    return sorted(block), sorted(warn)
