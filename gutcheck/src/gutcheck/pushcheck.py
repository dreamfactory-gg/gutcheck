"""House-rules check before `git push`: the unpushed diff against the project's standing rules (its CLAUDE.md files).

Deterministic checks first (secrets and secret files in the diff, merge-conflict markers, "never/don't `X`"
rules whose literal appears in added lines, and the repo's own ast-grep rules when it has an sgconfig.yml), then Jev over rule x diff hunk. Logging-only by default:
`[house_rules] mode = "log" | "on" | "off"`; "on" turns findings into a permission ask, never a block.

  gutcheck house-rules [--repo D] [--base REF] [--all]      dry run on the current branch's unpushed commits
Hook: check_push(inp) for PreToolUse Bash -> hook output dict or None.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gutcheck_core import config, jev, secretscan

LOG = config.STATE_DIR / "house_rules.jsonl"
RULE_CACHE = config.STATE_DIR / "house_rules_cache.json"
MAX_RULES = 40
MAX_HUNKS = 40
RULE_P = 0.5
VIOLATION_P = 0.7
CONFLICT_RE = re.compile(r"^(<{7} |={7}$|>{7} )")
NEG_RULE_RE = re.compile(r"\b(never|don'?t|do not|no|avoid|must not|mustn'?t|forbidden|not allowed)\b", re.I)


def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace", timeout=15)
    return r.stdout if r.returncode == 0 else None


# ---- is this a push? ----------------------------------------------------------------------------
def push_target(command, cwd):
    """(repo_dir, argv after 'push') for the first `git push` in the command, else None."""
    try:
        from gutcheck_core import shellparse
        cmds = [c.argv for c in shellparse.parse(command).cmds if not c.remote]
    except Exception:  # tree-sitter missing: plain split on shell separators
        import shlex
        cmds = []
        for part in re.split(r"&&|\|\||;|\|", command):
            try:
                cmds.append(shlex.split(part))
            except ValueError:
                continue
    for argv in cmds:
        if not argv or argv[0].rsplit("/", 1)[-1] != "git":
            continue
        i, repo = 1, cwd
        while i < len(argv) and argv[i].startswith("-"):
            if argv[i] == "-C" and i + 1 < len(argv):
                repo = os.path.join(repo or "", os.path.expanduser(argv[i + 1]))
                i += 2
            elif argv[i] in ("-c", "--git-dir", "--work-tree") and i + 1 < len(argv):
                i += 2
            else:
                i += 1
        if i < len(argv) and argv[i] == "push":
            return repo or os.getcwd(), argv[i + 1:]
    return None


def pending_base(repo, push_args=()):
    """The ref the push compares against: upstream, else the remote branch named in the push, else the remote's
    default branch (merge-base)."""
    up = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if up and up.strip():
        return up.strip()
    pos = [a for a in push_args if not a.startswith("-")]
    remote = pos[0] if pos else "origin"
    if len(pos) > 1:
        dst = pos[1].split(":")[-1].lstrip("+")
        if _git(repo, "rev-parse", "--verify", "-q", f"{remote}/{dst}"):
            return f"{remote}/{dst}"
    for ref in (f"{remote}/HEAD", f"{remote}/main", f"{remote}/master"):
        if _git(repo, "rev-parse", "--verify", "-q", ref):
            mb = _git(repo, "merge-base", "HEAD", ref)
            return mb.strip() if mb else None
    return None


# ---- rules --------------------------------------------------------------------------------------
def rule_files(root, changed_paths=()):
    root = Path(root)
    names = ["CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"]
    out = [root / n for n in names if (root / n).is_file()]
    dirs = set()
    for p in changed_paths:
        d = Path(p).parent
        while str(d) not in ("", "."):
            dirs.add(d)
            d = d.parent
    out += [root / d / "CLAUDE.md" for d in sorted(dirs) if (root / d / "CLAUDE.md").is_file()]
    return out


def _candidates(text):
    """[(first line no, text)] per bullet item or paragraph (wrapped lines joined), outside code fences."""
    units, cur, start, in_code = [], [], 0, False

    def flush():
        if cur:
            t = " ".join(cur)
            if len(t) >= 12:
                units.append((start, t[:800]))
        cur.clear()
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("```"):
            flush()
            in_code = not in_code
            continue
        if in_code:
            continue
        if not s or s.startswith(("#", "|", "<!--")):
            flush()
            continue
        bullet = re.match(r"^([-*+]|\d+[.)])\s+", s)
        if bullet or not cur:
            flush()
            start = n
        cur.append(re.sub(r"^([-*+]|\d+[.)])\s+", "", s))
    flush()
    return units


def rules(files, deadline=None):
    """[(source, rule text)], Jev-selected standing rules, cached per file content."""
    try:
        cache = json.loads(RULE_CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    out = []
    for f in files:
        text = f.read_text(errors="replace")
        key = "v2:" + hashlib.sha1(text.encode()).hexdigest()
        if key not in cache:
            cands = _candidates(text)
            if not cands:
                cache[key] = []
                continue
            probs = jev.noul_each([(n, secretscan.redact(t)[:600]) for n, t in cands],
                                  "Is this line a standing rule that a code change could break: a coding "
                                  "convention, a forbidden pattern or command, a file, naming, dependency or "
                                  "formatting rule, or a restriction on what may be committed? Descriptions of the "
                                  "project, background facts, and instructions about how to talk or plan are no.",
                                  deadline=deadline)
            cache[key] = [[n, t] for n, t in cands if probs.get(n, 0) >= RULE_P]
        out += [(f"{f.name}:{n}", t) for n, t in cache[key]]
    try:
        RULE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        RULE_CACHE.write_text(json.dumps(cache))
    except OSError:
        pass
    return out[:MAX_RULES]


# ---- diff ---------------------------------------------------------------------------------------
def _is_rule_file(path):
    return Path(path).name in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md")


def hunks(diff):
    """[(path, header, added_text)] with only added lines (what the push introduces)."""
    out, path, cur, header = [], None, [], ""
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if path and cur:
                out.append((path, header, "\n".join(cur)))
            m = re.match(r"diff --git a/.* b/(.*)$", line)
            path, cur, header = (m.group(1) if m else None), [], ""
        elif line.startswith("@@"):
            if path and cur:
                out.append((path, header, "\n".join(cur)))
            cur, header = [], line
        elif line.startswith("+") and not line.startswith("+++"):
            cur.append(line)
    if path and cur:
        out.append((path, header, "\n".join(cur)))
    return out


def deterministic(diff, files_added, rule_list):
    found = []
    for p in files_added:
        if secretscan.is_secret_file(p):
            found.append(("secret file", p, f"adds {p}"))
    for path, header, added in hunks(diff):
        for line in added.splitlines():
            body = line[1:]
            if CONFLICT_RE.match(body):
                found.append(("conflict marker", path, body[:40]))
            for kind, _, _ in secretscan.find(body, high_entropy=False):
                found.append(("secret", path, f"{kind}-shaped value added"))
    for src, rule in rule_list:  # "never `X`" rules: the literal in added lines is a deterministic hit
        if not NEG_RULE_RE.search(rule):
            continue
        for lit in re.findall(r"`([^`]{3,60})`", rule):
            for path, header, added in hunks(diff):
                if _is_rule_file(path):
                    continue
                body = "\n".join(l[1:] for l in added.splitlines())
                pat = rf"(?<![\w-]){re.escape(lit)}(?![\w-])" if re.fullmatch(r"[\w.\-]+", lit) else re.escape(lit)
                if re.search(pat, body):
                    found.append(("rule literal", path, f"{src}: adds `{lit}` ({rule[:80]})"))
                    break
    return found


def ast_grep_hits(root, changed, timeout):
    """([(kind, path, why)], note): warning and error hits of the repo's ast-grep rules over the changed files that
    still exist; note says why nothing ran (no ast-grep on PATH or in node_modules/.bin, or a failed scan)."""
    files = [p for p in changed if (Path(root) / p).is_file()]
    exe = shutil.which("ast-grep", path=os.pathsep.join([str(Path(root) / "node_modules" / ".bin"),
                                                         os.environ.get("PATH", "")]))
    if not exe:
        return [], "ast-grep not found; sgconfig.yml rules skipped"
    if not files:
        return [], None
    try:  # exits 1 when an error-severity rule hits; stdout holds the matches either way
        r = subprocess.run([exe, "scan", "--json=compact", *files], cwd=root, capture_output=True, text=True,
                           timeout=timeout)
        found = json.loads(r.stdout or "[]")
        hits = [("ast-grep", m["file"], f"{m['ruleId']} (line {m['range']['start']['line'] + 1}): {m['message']}")
                for m in found if m["severity"] in ("warning", "error")]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as e:
        return [], f"ast-grep: {type(e).__name__}"
    if r.returncode and not found:
        return [], f"ast-grep: scan failed (exit {r.returncode}): {r.stderr.strip().partition('\n')[0][:150]}"
    return hits, None


def jev_violations(rule_list, hunk_list, deadline=None):
    if not rule_list or not hunk_list:
        return []
    qs = {f"r{i}": {"type": "noul", "instructions":
                    f"Project rule: \"{rule}\". Do the added lines (starting with +) in state.hunk clearly break this "
                    f"rule? Answer no if the rule is about something this hunk doesn't touch, or if it's unclear."}
          for i, (_, rule) in enumerate(rule_list)}

    def one(h):
        path, header, added = h
        state = {"file": path, "hunk": secretscan.redact(f"{header}\n{added}")[:6000]}
        a = jev.call(state, qs, deadline=deadline)
        return [(path, header, rule_list[int(k[1:])], a[k]["noul"]) for k in a if a[k]["noul"] >= VIOLATION_P]

    out = []
    with ThreadPoolExecutor(8) as ex:
        for part in ex.map(one, hunk_list[:MAX_HUNKS]):
            out += part
    return out


def check_repo(repo, push_args=(), base=None, deadline=None):
    root = _git(repo, "rev-parse", "--show-toplevel")
    if not root:
        return {"skipped": "not a git repo"}
    root = root.strip()
    base = base or pending_base(root, push_args)
    if not base:
        return {"skipped": "no upstream or remote base to compare with"}
    diff = _git(root, "diff", "--no-color", f"{base}...HEAD") or ""
    if not diff.strip():
        return {"skipped": "nothing unpushed", "base": base}
    names = (_git(root, "diff", "--name-only", "--diff-filter=A", f"{base}...HEAD") or "").split()
    changed = (_git(root, "diff", "--name-only", f"{base}...HEAD") or "").split()
    rf = rule_files(root, changed)
    t0 = time.monotonic()

    def left():  # what remains of the deadline for the next step (rules, ast-grep and violations share it)
        return None if deadline is None else max(1.0, deadline - (time.monotonic() - t0))
    rule_list, err = [], None
    try:
        rule_list = rules(rf, deadline=deadline)
    except jev.JevError as e:
        err = f"rules: {str(e)[:150]}"
    det = deterministic(diff, names, rule_list)
    sg_note = None
    if (Path(root) / "sgconfig.yml").is_file():
        sg, sg_note = ast_grep_hits(root, changed, timeout=left())
        det += sg
    hl = [h for h in hunks(diff) if not _is_rule_file(h[0])]
    viol = []
    if rule_list and not err:
        try:
            viol = jev_violations(rule_list, hl, deadline=left())
        except jev.JevError as e:
            err = f"violations: {str(e)[:150]}"
    return {"repo": root, "base": base, "rule_files": [str(p) for p in rf], "rules": len(rule_list),
            "hunks": len(hl), "deterministic": det,
            "violations": [{"file": p, "hunk": h[:60], "rule_src": r[0], "rule": r[1][:200], "p": round(pr, 2)}
                           for p, h, r, pr in viol], "jev_error": err, "ast_grep": sg_note, "ms": int((time.monotonic() - t0) * 1000)}


def summary(res, limit=3):
    items = [f"{k}: {p} ({why})" for k, p, why in res.get("deterministic", [])]
    items += [f"{v['file']} may break {v['rule_src']} \"{v['rule'][:90]}\" (p={v['p']})"
              for v in res.get("violations", [])]
    if not items:
        return None
    more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return "gutcheck house rules, before this push: " + "; ".join(items[:limit]) + more


def check_push(inp):
    """PreToolUse Bash: only acts on `git push`. Logging-only unless [house_rules] mode = "on" (then: ask)."""
    cmd = (inp.get("tool_input") or {}).get("command", "")
    if "push" not in cmd or "git" not in cmd:
        return None
    tgt = push_target(cmd, inp.get("cwd"))
    if not tgt:
        return None
    cfg = config.load().get("house_rules", {})
    mode = cfg.get("mode", "log")
    if mode == "off":
        return None
    repo, args = tgt
    res = check_repo(repo, args, deadline=float(cfg.get("deadline_s", 20)))
    msg = summary(res) if "skipped" not in res else None
    _log({"session": inp.get("session_id"), "mode": mode, "cmd": secretscan.redact(cmd)[:300], **res,
          "message": msg})
    if msg and mode == "on":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                       "permissionDecisionReason": msg}}
    return None


def _log(rec):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}) + "\n")
    except OSError:
        pass


def main(args):
    from gutcheck_core.args import _opt
    repo = _opt(args, "--repo", os.getcwd())
    base = _opt(args, "--base")
    res = check_repo(repo, base=base)
    if "skipped" in res:
        print(f"gutcheck house-rules: skipped ({res['skipped']})")
        return
    print(f"# {res['rules']} rules from {', '.join(Path(p).name for p in res['rule_files']) or 'no CLAUDE.md'}; "
          f"{res['hunks']} hunks vs {res['base']}; {res['ms']} ms" + "".join(f"; {res[k]}" for k in ("jev_error", "ast_grep") if res[k]))
    for k, p, why in res["deterministic"]:
        print(f"[{k}] {p}: {why}")
    for v in res["violations"]:
        print(f"[rule p={v['p']}] {v['file']} {v['hunk']}\n    {v['rule_src']}: {v['rule']}")
    if not res["deterministic"] and not res["violations"]:
        print("no findings")


def hook(inp):
    """HANDLERS entry for the PrePush hook (PreToolUse Bash, filtered to git): prints the hook output, if any."""
    out = check_push(inp)
    if out:
        sys.stdout.write(json.dumps(out))
