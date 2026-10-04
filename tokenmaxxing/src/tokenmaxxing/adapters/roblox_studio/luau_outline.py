"""Deterministic outline of Luau/Lua source: requires, types, module tables, functions and event
handlers with their line ranges. No bodies. Used to shrink large Studio script reads."""
import re

_DEF = [
    ("fn", re.compile(r"^(\s*)(local\s+)?function\s+([\w.:]+)\s*(<[^>]*>)?\s*\(([^)]*)\)")),
    ("fn", re.compile(r"^(\s*)(local\s+)?([\w.]+)\s*=\s*function\s*\(([^)]*)\)")),
    ("type", re.compile(r"^(\s*)(export\s+)?type\s+(\w+)")),
    ("require", re.compile(r"^(\s*)local\s+(\w+)\s*=\s*require\((.+)\)")),
    ("table", re.compile(r"^()local\s+(\w+)\s*(:\s*[^=]+)?=\s*(\{\s*\}|setmetatable\()")),
    ("connect", re.compile(r"^(\s*)([\w.:\[\]\"']+):Connect\(\s*function\s*\(([^)]*)\)")),
    ("const", re.compile(r"^()local\s+([A-Z][A-Z0-9_]{2,})\s*(:\s*[^=]+)?=")),
]
_BLOCK_OPEN = re.compile(r"\b(function|do|then)\b")
_END = re.compile(r"^\s*end\b")


def strip_numbers(text):
    """script_read output is `   N→code`; return plain source lines."""
    out = []
    for ln in text.splitlines():
        m = re.match(r"^\s*\d+→(.*)$", ln)
        out.append(m.group(1) if m else ln)
    return out


def outline(lines, max_indent=8):
    """Return [(start, end, kind, text)] with 1-based line numbers."""
    items = []
    for i, ln in enumerate(lines, 1):
        if not ln.strip() or ln.lstrip().startswith("--"):
            continue
        for kind, rx in _DEF:
            m = rx.match(ln)
            if m:
                indent = len(m.group(1).expandtabs(4))
                if indent > max_indent:
                    break
                items.append([i, i, kind, indent, ln.strip()[:160]])
                break
    # end line of each function/handler: first `end` at the same indentation after it
    for it in items:
        if it[2] not in ("fn", "connect"):
            continue
        start, indent = it[0], it[3]
        for j in range(start, len(lines)):
            ln = lines[j]
            if _END.match(ln) and len(ln) - len(ln.lstrip()) <= indent and len(ln[:len(ln) - len(ln.lstrip())].expandtabs(4)) == indent:
                it[1] = j + 1
                break
            if ln.strip().startswith("end)") and len(ln[:len(ln) - len(ln.lstrip())].expandtabs(4)) == indent:
                it[1] = j + 1
                break
    return [(s, e, k, t) for s, e, k, _, t in items]


def render(lines, path=None, title=None):
    items = outline(lines)
    head = f"-- {title or 'script'}: {len(lines)} lines; outline only (signatures and line ranges)."
    if path:
        head += f"\n-- Full source saved to {path} (Read it with offset/limit for the ranges you need)."
    body = []
    # spans not covered by a listed range: top-level statements, listed so every line is reachable
    covered = set()
    for s, e, k, t in items:
        covered.update(range(s, e + 1))
    gaps, start = [], None
    for i in range(1, len(lines) + 1):
        if i not in covered and lines[i - 1].strip():
            start = start or i
        elif start and (i in covered):
            gaps.append((start, i - 1))
            start = None
    if start:
        gaps.append((start, len(lines)))
    items = sorted(items + [(a, b, "gap", "(top-level statements)") for a, b in gaps if b - a >= 3])
    for s, e, k, t in items:
        rng = f"L{s}" if s == e else f"L{s}-{e}"
        body.append(f"{rng:>10}  {t}")
    return head + "\n" + "\n".join(body)
