"""Shared relevance pass: pick the files and ranges that matter for a change, once, and write them to a file
that every background check (review, test-gap, docs-drift, explainer) reuses instead of exploring again.

  tokenmaxxing relevance [--repo DIR] [--commit SHA | --base REF] [--focus TEXT] [--json]

Deterministic first (git diff, changed symbols from the outline, `git grep` for callers/tests/docs), then one
Jev pass over the candidate files and, for files with many hits, over their outline entries. Output:
STATE_DIR/relevance/<repo>-<key>.json (+ .md), where key hashes the diff, so the same change is selected once.
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from gutcheck_core import config, jev, secretscan

OUT_DIR = config.STATE_DIR / "relevance"
MAX_SYMBOLS = 40
MAX_CANDIDATES = 120
DOC_EXT = (".md", ".mdx", ".rst", ".txt", ".adoc")
NON_CODE_EXT = (".json", ".lock", ".log", ".csv", ".svg", ".html", ".htm", ".xml", ".yaml", ".yml", ".toml")
MAX_RELATED = {"code": 15, "test": 10, "doc": 8}
TEST_RE = re.compile(r"(^|/)(tests?|spec|__tests__)(/|$)|(^|/)test_[^/]*$|_test\.\w+$|\.test\.\w+$|\.spec\.\w+$|Tests?\.\w+$")
GENERIC = {"main", "run", "init", "get", "set", "test", "setup", "update", "load", "save", "start", "stop", "call",
           "log", "new", "self", "data", "name", "value", "args", "config", "__init__", "cli", "handle", "parse"}
DEF_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|function|class|fn|func|struct|interface|type|enum|"
                    r"local\s+function|pub\s+fn|const|let|var)\s+([A-Za-z_][\w.:]*)")


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
VENDORED = re.compile(r"(^|/)(node_modules|site-packages|dist-packages|vendor|\.?venv[^/]*)/")


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), "-c", "core.quotePath=false", *args], capture_output=True, text=True,
                       errors="replace")
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout


def repo_root(path="."):
    return Path(git(path, "rev-parse", "--show-toplevel").strip())


def resolve_scope(repo, commit=None, base=None):
    """(label, diff_args). Default: uncommitted changes vs HEAD if any, else the last commit."""
    if commit:
        sha = git(repo, "rev-parse", "--short", commit).strip()
        return f"commit {sha}", ["show", "--format=", "--no-color", sha]
    if base:
        mb = git(repo, "merge-base", base, "HEAD").strip()
        return f"{base}...HEAD (+ working tree)", ["diff", "--no-color", mb]
    if not git(repo, "rev-parse", "-q", "--verify", "HEAD", check=False).strip():
        return "working tree (no commits yet)", ["diff", "--no-color", EMPTY_TREE]
    if git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        return "working tree vs HEAD", ["diff", "--no-color", "HEAD"]
    sha = git(repo, "rev-parse", "--short", "HEAD").strip()
    return f"commit {sha}", ["show", "--format=", "--no-color", sha]


def untracked_as_diff(repo, max_bytes=60_000):
    out = []
    for p in git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
        f = Path(repo) / p
        if secretscan.is_secret_file(p) or VENDORED.search(p) or not f.is_file() or f.stat().st_size > max_bytes:
            continue
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        out.append(f"diff --git a/{p} b/{p}\nnew file\n--- /dev/null\n+++ b/{p}\n@@ -0,0 +1,{len(lines)} @@\n" +
                   "\n".join("+" + l for l in lines))
    return "\n".join(out)


def get_diff(repo, commit=None, base=None):
    label, args = resolve_scope(repo, commit, base)
    text = git(repo, *args)
    if args[0] == "diff":
        extra = untracked_as_diff(repo)
        if extra:
            text += ("\n" if text else "") + extra
    return label, text


CALLABLE_RE = re.compile(r"\b(def|function|class|fn|func|struct|interface|enum)\b|=>|\bfunction\s*\(")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


def parse_diff(text):
    """[{path, old_path, added, removed, hunks: [{new_start, new_len, old_start, header, lines}]}]"""
    files, cur, hunk = [], None, None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = {"path": m.group(2) if m else line[11:], "old_path": m.group(1) if m else None,
                   "added": 0, "removed": 0, "hunks": [], "deleted": False, "binary": False}
            files.append(cur)
            hunk = None
        elif cur is None:
            continue
        elif line.startswith("deleted file"):
            cur["deleted"] = True
        elif line.startswith("Binary files"):
            cur["binary"] = True
        elif line.startswith(("--- ", "+++ ")) and hunk is None:
            continue
        elif (m := _HUNK.match(line)):
            hunk = {"old_start": int(m.group(1)), "new_start": int(m.group(3)), "new_len": int(m.group(4) or 1),
                    "header": m.group(5).strip(), "lines": []}
            cur["hunks"].append(hunk)
        elif hunk is not None:
            hunk["lines"].append(line)
            if line.startswith("+"):
                cur["added"] += 1
            elif line.startswith("-"):
                cur["removed"] += 1
    return files


_OUT_LINE = re.compile(r"^(\s*)(.+?)\s+L(\d+)(?:-(\d+))?\s*$")


def outline(path):
    """[(start, end, signature, indent)] from ast-outline, or a regex fallback. Never raises."""
    exe = shutil.which("ast-outline")
    rows = []
    if exe:
        try:
            r = subprocess.run([exe, str(path)], capture_output=True, text=True, timeout=20, errors="replace")
            for line in r.stdout.splitlines():
                m = _OUT_LINE.match(line)
                if m and not m.group(2).startswith(("#", '"""', "'''")):
                    s = int(m.group(3))
                    rows.append((s, int(m.group(4) or s), m.group(2).strip(), len(m.group(1))))
        except (OSError, subprocess.TimeoutExpired):
            rows = []
    if not rows:
        try:
            lines = Path(path).read_text(errors="replace").splitlines()
        except OSError:
            return []
        starts = [(n + 1, m.group(0).strip()) for n, l in enumerate(lines) if (m := DEF_RE.match(l))]
        for i, (s, sig) in enumerate(starts):
            e = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(lines)
            rows.append((s, e, sig[:160], 0))
    return rows


def symbol_name(sig):
    m = re.search(r"(?:def|function|class|fn|func|struct|interface|type|enum|const|let|var)\s+([A-Za-z_][\w.:]*)", sig)
    if m:
        return re.split(r"[.:]", m.group(1))[-1]
    m = re.match(r"([A-Za-z_]\w*)", sig)
    return m.group(1) if m else None


def enclosing(rows, line):
    """Innermost outline entry containing `line`."""
    best = None
    for s, e, sig, ind in rows:
        if s <= line <= e and (best is None or (e - s) <= (best[1] - best[0])):
            best = (s, e, sig, ind)
    return best


def outline_at(repo, path, rev=None):
    """Outline of `path` as of `rev` (None = working tree)."""
    if rev is None:
        p = Path(repo) / path
        return outline(p) if p.is_file() else []
    import tempfile
    r = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True)
    if r.returncode:
        return []
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / Path(path).name
        tmp.write_bytes(r.stdout)
        return outline(tmp)


def changed_symbols(repo, f, rev=None):
    """Symbols whose bodies the diff touches (new-side line numbers), from the file's outline."""
    rows = outline_at(repo, f["path"], rev)
    touched = {}
    for h in f["hunks"]:
        n = h["new_start"]
        for l in h["lines"]:
            if l.startswith("-"):
                hit = enclosing(rows, max(n, 1))
            elif l.startswith("+"):
                hit = enclosing(rows, n)
                n += 1
            else:
                n += 1
                continue
            if hit:
                name = symbol_name(hit[2])
                if name:
                    touched.setdefault((hit[0], hit[1]), (name, hit[2]))
        m = DEF_RE.match(h["header"]) if h["header"] else None
        if m and not rows:
            touched.setdefault((h["new_start"], h["new_start"] + h["new_len"]), (m.group(1).split(".")[-1], h["header"]))
    # removed definitions (the name may still be referenced elsewhere)
    for h in f["hunks"]:
        for l in h["lines"]:
            if l.startswith("-") and (m := DEF_RE.match(l[1:])):
                name = m.group(1).split(".")[-1].split(":")[-1]
                if name not in {v[0] for v in touched.values()}:
                    touched.setdefault((0, 0, name), (name, l[1:].strip()[:160]))
    out = []
    for k, (name, sig) in touched.items():
        out.append({"name": name, "range": [k[0], k[1]] if len(k) == 2 else None, "signature": sig[:160]})
    return out


def _grep(repo, words, rev=None, limit=4000):
    """{path: [(line, text, word)]} for whole-word matches of `words` in tracked files (at `rev` if given)."""
    if not words:
        return {}
    args = ["grep", "-n", "-w", "-I", "--no-color"]
    for w in words:
        args += ["-e", w]
    if rev:
        args.append(rev)
    out = {}
    for line in git(repo, *args, check=False).splitlines()[:limit]:
        if rev and line.startswith(rev + ":"):
            line = line[len(rev) + 1:]
        parts = line.split(":", 2)
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        path, n, text = parts
        w = next((w for w in words if re.search(rf"\b{re.escape(w)}\b", text)), "")
        out.setdefault(path, []).append((int(n), text.strip()[:200], w))
    return out


def summary_text(files, symbols, focus=None, max_chars=6000):
    """A compact description of the change, used as Jev context and in the .md file."""
    parts = []
    if focus:
        parts.append(f"Focus: {focus}")
    for f in files:
        syms = ", ".join(s["name"] for s in symbols.get(f["path"], [])[:12])
        parts.append(f"{f['path']} (+{f['added']} -{f['removed']}){': ' + syms if syms else ''}")
    body = []
    for f in files:
        for h in f["hunks"]:
            body.append(f"@@ {f['path']}:{h['new_start']} {h['header']}")
            body.extend(l for l in h["lines"] if l.startswith(("+", "-")) and l[1:].strip())
    text = "\n".join(parts) + "\n\n" + "\n".join(body)
    return secretscan.redact(text[:max_chars])


def build(repo=".", commit=None, base=None, focus=None, reuse=True, min_p=0.35, deadline=None):
    repo = repo_root(repo)
    label, diff = get_diff(repo, commit, base)
    key = hashlib.sha1((str(repo) + "\0" + diff + "\0" + (focus or "")).encode()).hexdigest()[:12]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_json = OUT_DIR / f"{repo.name}-{key}.json"
    if reuse and out_json.exists():
        return json.loads(out_json.read_text()), out_json
    rev = git(repo, "rev-parse", commit).strip() if commit else None
    files = [f for f in parse_diff(diff) if not f["binary"]]
    symbols = {f["path"]: changed_symbols(repo, f, rev) for f in files
               if not f["deleted"] and not f["path"].lower().endswith(DOC_EXT + NON_CODE_EXT)}
    changed_paths = {f["path"] for f in files}

    names, consts, test_defs = [], [], set()
    for f in files:
        if TEST_RE.search(f["path"]):
            test_defs.update(s["name"] for s in symbols.get(f["path"], []))
        for s in symbols.get(f["path"], []):
            n = s["name"]
            if n in names or n in consts or n in GENERIC or len(n) <= 2:
                continue
            (names if CALLABLE_RE.search(s["signature"]) else consts).append(n)
    names += [c for c in consts if not c.isupper()]
    stems = sorted({Path(f["path"]).stem for f in files if len(Path(f["path"]).stem) > 3} - GENERIC)
    hits = _grep(repo, names[:MAX_SYMBOLS] + stems, rev)
    cands = {p: h for p, h in hits.items() if p not in changed_paths and not secretscan.is_secret_file(p)}
    # tests named after a changed file count even without a symbol hit
    tracked = git(repo, "ls-tree", "-r", "--name-only", rev, check=False) if rev else git(repo, "ls-files", check=False)
    for p in tracked.splitlines():
        if TEST_RE.search(p) and any(Path(p).stem in (f"test_{s}", f"{s}_test", f"{s}.test", f"{s}.spec") for s in stems):
            cands.setdefault(p, [])
    ranked = sorted(cands.items(), key=lambda kv: -len({w for _, _, w in kv[1]}))[:MAX_CANDIDATES]

    context = summary_text(files, symbols, focus)
    items = []
    for p, h in ranked:
        sample = "\n".join(f"{n}: {t}" for n, t, _ in h[:12])
        items.append((p, secretscan.redact(f"file: {p}\n{sample}")))
    probs = {}
    jev_error = None
    if items:
        try:
            probs = jev.noul_each(items, "Does this file call, get called by, import, test or document the code "
                                         "changed in context.change, so that someone reviewing the change would "
                                         "need to look at it? The listed lines are where it mentions the changed "
                                         "names. A mention of an unrelated thing with the same name is no. A log, "
                                         "review verdict, report or transcript that only mentions the code in "
                                         "passing is no; docs describing current behaviour are yes.",
                                  context={"change": context}, deadline=deadline)
        except jev.JevError as e:
            jev_error = str(e)[:300]
            probs = {p: 1.0 for p, _ in items}  # fail open: keep every candidate

    docs = [(p, t) for p, t in items if p.lower().endswith(DOC_EXT) and probs.get(p, 0) >= min_p]
    if docs and not jev_error:
        try:
            cur = jev.noul_each(docs, "Is this file documentation of how the software currently works or is used "
                                      "(README, guide, API or design doc, contract, spec), as opposed to a "
                                      "historical record (review, verdict, audit, report, prompt, log, "
                                      "transcript, handoff, dated notes)? Judge from the path and lines shown.",
                                deadline=deadline)
            for p, pc in cur.items():
                if pc < 0.5:
                    probs[p] = min(probs[p], pc)
        except jev.JevError:
            pass
    related = []
    for p, h in ranked:
        pr = probs.get(p, 0.0)
        if pr < min_p:
            continue
        kind = "test" if TEST_RE.search(p) else "doc" if p.lower().endswith(DOC_EXT) else "code"
        ranges = []
        if kind != "doc":
            rows = outline_at(repo, p, rev)
            seen = set()
            for n, _, w in h:
                e = enclosing(rows, n)
                if e and (e[0], e[1]) not in seen:
                    seen.add((e[0], e[1]))
                    ranges.append([e[0], e[1], e[2][:120], w])
            if len(ranges) > 3:
                ranges = _prune_ranges(p, ranges, context, deadline)
        else:
            ranges = [[n, n, t[:120], w] for n, t, w in h[:8]]
        related.append({"path": p, "p": round(pr, 2), "kind": kind, "hits": len(h), "ranges": ranges[:12]})
    related.sort(key=lambda r: -r["p"])
    per_kind = {}
    related = [r for r in related if (per_kind.setdefault(r["kind"], []).append(r) or
                                      len(per_kind[r["kind"]]) <= MAX_RELATED[r["kind"]])]

    tested = {w for r in related if r["kind"] == "test" for _, _, w in cands.get(r["path"], [])}
    res = {
        "repo": str(repo), "scope": label, "key": key, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "focus": focus, "commit": commit, "base": base,
        "changed": [{"path": f["path"], "added": f["added"], "removed": f["removed"], "deleted": f["deleted"],
                     "symbols": symbols.get(f["path"], [])} for f in files],
        "related": related,
        "tests": [r["path"] for r in related if r["kind"] == "test"] +
                 [f["path"] for f in files if TEST_RE.search(f["path"])],
        "docs": [r["path"] for r in related if r["kind"] == "doc"] +
                [f["path"] for f in files if f["path"].lower().endswith(DOC_EXT)],
        "untested_symbols": [n for n in names if n not in tested and n not in consts and n not in test_defs and not n.startswith("_") and
                             not any(TEST_RE.search(f["path"]) and n in _added_text(f) for f in files)],
        "candidates": len(items), "jev_error": jev_error,
    }
    out_json.write_text(json.dumps(res, indent=1))
    out_json.with_suffix(".md").write_text(render_md(res))
    (OUT_DIR / f"latest-{repo.name}.json").write_text(json.dumps({"path": str(out_json)}))
    return res, out_json


def _added_text(f):
    return "\n".join(l for h in f["hunks"] for l in h["lines"] if l.startswith("+"))


def _prune_ranges(path, ranges, context, deadline):
    items = [(i, f"{path}:{s}-{e} {sig}") for i, (s, e, sig, _) in enumerate(ranges)]
    try:
        pr = jev.noul_each(items, "Is this definition likely to use, call, test or document the code changed in "
                                  "context.change?", context={"change": context[:3000]}, deadline=deadline)
    except jev.JevError:
        return ranges
    kept = [r for i, r in enumerate(ranges) if pr.get(i, 1) >= 0.35]
    return kept or ranges


def render_md(res):
    out = [f"# Relevance pass: {Path(res['repo']).name} ({res['scope']})", ""]
    if res.get("focus"):
        out += [f"Focus: {res['focus']}", ""]
    out.append("## Changed")
    for c in res["changed"]:
        syms = "; ".join(f"{s['name']}" + (f" L{s['range'][0]}-{s['range'][1]}" if s.get("range") else " (removed)")
                         for s in c["symbols"][:15])
        out.append(f"- {c['path']} (+{c['added']} -{c['removed']}){' — ' + syms if syms else ''}")
    out += ["", "## Related (callers, tests, docs)"]
    for r in res["related"]:
        rng = ", ".join(f"{s}-{e} {sig}" for s, e, sig, _ in r["ranges"][:6])
        out.append(f"- [{r['kind']}] {r['path']} (p={r['p']}){': ' + rng if rng else ''}")
    if not res["related"]:
        out.append("- none found")
    if res["untested_symbols"]:
        out += ["", "## Changed symbols no test mentions", "- " + ", ".join(res["untested_symbols"][:30])]
    if res.get("jev_error"):
        out += ["", f"(Jev unavailable, kept every candidate: {res['jev_error'][:120]})"]
    return "\n".join(out) + "\n"


def load_latest(repo):
    p = OUT_DIR / f"latest-{Path(repo).name}.json"
    if p.exists():
        path = Path(json.loads(p.read_text())["path"])
        if path.exists():
            return json.loads(path.read_text()), path
    return None, None


def main(args):
    from gutcheck_core.args import _flag, _opt
    repo = _opt(args, "--repo", ".")
    commit = _opt(args, "--commit")
    base = _opt(args, "--base")
    focus = _opt(args, "--focus")
    as_json = _flag(args, "--json")
    fresh = _flag(args, "--fresh")
    res, path = build(repo, commit, base, focus, reuse=not fresh)
    if as_json:
        print(json.dumps(res, indent=1))
    else:
        sys.stdout.write(render_md(res))
        print(f"\n# saved: {path} (and .md)", file=sys.stderr)
