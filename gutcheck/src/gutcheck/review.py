"""Background checks when a reviewable unit is done. They only read, never block and are never a gate.

  gutcheck review --background "FOCUS" [--repo D] [--commit SHA | --base REF]   start detached, print where results land
  gutcheck review --wait "FOCUS" ...                                              same, in the foreground
  gutcheck review --status [ID]                                                   recent jobs and their result files

One relevance pass (relevance.py) feeds all three checks:
  review.md      adversarial cross-model review by Codex (its config's default model/effort), through the Codex
                 plugin's companion script when installed, else `codex exec --sandbox read-only`
  test-gap.md    changed functions no test mentions (deterministic, from the relevance pass)
  docs-drift.md  doc paragraphs that mention changed names and may now be stale (code for old numbers, Jev for the rest)
  SUMMARY.md     one line per check with paths
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config, jev, jobs, relevance, secretscan

LOG = config.STATE_DIR / "reviews.jsonl"


# ---- adversarial review (Codex) ------------------------------------------------------------------
def companion():
    """Newest installed codex-companion.mjs from the Codex plugin, or None."""
    base = Path(os.environ.get("CLAUDE_PLUGINS_DIR", Path.home() / ".claude" / "plugins")) / "cache"
    found = glob.glob(str(base / "*" / "codex" / "*" / "scripts" / "codex-companion.mjs"))

    def ver(p):
        v = Path(p).parent.parent.name
        return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v)]
    return max(found, key=ver) if found else None


def _focus_text(focus, rel, rel_md_path):
    return (f"{focus}\n\nThe relevant files and ranges for this change were already selected (start there instead "
            f"of exploring the whole repo; full list in {rel_md_path}):\n{relevance.render_md(rel)[:6000]}")


def adversarial(rel, rel_md_path, focus, commit, base, out):
    repo = rel["repo"]
    text = _focus_text(focus, rel, rel_md_path)
    node, comp = shutil.which("node"), companion()
    head = relevance.git(repo, "rev-parse", "HEAD").strip()
    target_is_head = commit and relevance.git(repo, "rev-parse", commit).strip() == head
    if node and comp and (not commit or target_is_head):
        cmd = [node, comp, "adversarial-review", "--wait"]
        if base:
            cmd += ["--base", base]
        elif target_is_head:
            cmd += ["--base", f"{head}~1"]
        cmd.append(text)
        via = "codex-companion adversarial-review"
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, errors="replace")
        body = r.stdout.strip() or r.stderr.strip()[-3000:]
    else:
        codex = shutil.which("codex")
        if not codex:
            out.write_text("Skipped: neither the Codex plugin's companion script nor the codex CLI is installed.\n")
            return "skipped (no codex)", 0
        _, diff = relevance.get_diff(repo, commit, base)
        prompt = (f"Adversarial review, read-only. Your job is to break confidence in this change: find the strongest "
                  f"reasons it should not ship (data loss, races, retries and partial failure, trust boundaries, "
                  f"null/timeout paths, compatibility). Report only material findings, each with file, line range, "
                  f"confidence 0-1, what goes wrong, why, impact, and a concrete fix. End with a one-line ship/no-ship "
                  f"verdict. Markdown, compact.\n\nTarget: {rel['scope']} in {repo}\nFocus: {text}\n\n"
                  f"```diff\n{secretscan.redact(diff)[:120_000]}\n```")
        last = out.with_suffix(".last.txt")
        via = "codex exec (read-only)"
        r = subprocess.run([codex, "exec", "-C", repo, "--sandbox", "read-only", "--skip-git-repo-check",
                            "--color", "never", "-o", str(last), "-"], input=prompt, capture_output=True, text=True,
                           errors="replace")
        body = last.read_text(errors="replace").strip() if last.exists() else r.stderr[-3000:]
    out.write_text(f"# Adversarial review ({via})\n\n{body}\n")
    verdict = "needs-attention" if re.search(r"needs-attention|no-ship|do not ship", body, re.I) else \
        "approve" if re.search(r"\bapprove\b|\bship\b", body, re.I) else "see file"
    return f"{verdict} (exit {r.returncode})", r.returncode


# ---- test gap -----------------------------------------------------------------------------------
def test_gap(rel, out):
    code = [c for c in rel["changed"] if not relevance.TEST_RE.search(c["path"]) and c["symbols"]
            and not c["path"].lower().endswith(relevance.DOC_EXT + relevance.NON_CODE_EXT)]
    lines = ["# Test gap", "", f"Scope: {rel['scope']}. Tests related to the change: "
             f"{', '.join(rel['tests']) or 'none found'}.", ""]
    untested = rel.get("untested_symbols") or []
    if untested:
        lines.append("Changed functions/classes that no test file mentions (by name). Don't write unit tests for these by "
                     "default: cover them with lint/type checks and the unit's end-to-end run; add a unit test only for a "
                     "small, crucial piece whose failure the end-to-end run wouldn't show.")
        by_file = {}
        for c in code:
            for s in c["symbols"]:
                if s["name"] in untested and s.get("range"):
                    by_file.setdefault(c["path"], []).append(f"{s['name']} L{s['range'][0]}-{s['range'][1]}")
        for p, syms in by_file.items():
            lines.append(f"- {p}: {', '.join(syms[:15])}")
    else:
        lines.append("Every changed function/class is mentioned by some test (by name).")
    out.write_text("\n".join(lines) + "\n")
    return f"{len(untested)} changed symbol(s) no test mentions" if untested else "no gaps by name"


# ---- docs drift ---------------------------------------------------------------------------------
def _old_values(repo, rel, commit, base):
    """{name: [old numbers]} from assignments whose numbers changed in the diff."""
    _, diff = relevance.get_diff(repo, commit, base)
    rem, add = {}, {}
    for line in diff.splitlines():
        m = re.match(r"^([+-])\s*(?:[\w.]+\s*[:=]\s*)?.*?\b([A-Za-z_][\w]{3,})\b\s*[:=]\s*[^=]*?(\d[\d_.]*)", line)
        if m and not line.startswith(("+++", "---")):
            (rem if m.group(1) == "-" else add).setdefault(m.group(2), set()).add(m.group(3).replace("_", ""))
    return {k: sorted(v - add.get(k, set())) for k, v in rem.items() if v - add.get(k, set())}


def docs_drift(rel, out, commit, base, deadline=None):
    repo = Path(rel["repo"])
    names = sorted({s["name"] for c in rel["changed"] for s in c["symbols"] if len(s["name"]) > 3})
    olds = _old_values(repo, rel, commit, base)
    changed_docs = {c["path"] for c in rel["changed"] if c["path"].lower().endswith(relevance.DOC_EXT)}
    docs = [d for d in dict.fromkeys(rel["docs"] + ["README.md"]) if d not in changed_docs and (repo / d).is_file()]
    paras = []
    for d in docs[:12]:
        text = (repo / d).read_text(errors="replace")
        n = 1
        for block in re.split(r"\n\s*\n", text):
            hit = [w for w in names + list(olds) if re.search(rf"\b{re.escape(w)}\b", block)]
            if hit:
                paras.append((f"{d}:{n}", block[:1500], hit))
            n += block.count("\n") + 2
    flagged = []
    for key, block, hit in paras:  # deterministic: an old value next to its name
        for w in hit:
            for v in olds.get(w, []):
                if re.search(rf"\b{re.escape(w)}\b[^\n]{{0,80}}\b{re.escape(v)}\b", block):
                    flagged.append((key, f"mentions {w} with its old value {v}", 1.0))
    if paras:
        change = "\n".join(f"{c['path']}: " + ", ".join(s["name"] for s in c["symbols"][:10]) for c in rel["changed"])
        try:
            _, diff = relevance.get_diff(repo, commit, base)
            probs = jev.noul_each([(k, b) for k, b, _ in paras],
                                  "Does this documentation paragraph describe behaviour, a default, a limit, a name "
                                  "or a usage that the change in context.change alters, so that the paragraph is "
                                  "now out of date? Mentioning the same names is not enough; it must state "
                                  "something the change makes untrue.",
                                  context={"change": secretscan.redact(change + "\n\n" + diff[:20_000])},
                                  deadline=deadline)
            for k, b, hit in paras:
                if probs.get(k, 0) >= 0.6 and not any(f[0] == k for f in flagged):
                    flagged.append((k, f"may be stale (mentions {', '.join(hit[:3])})", probs[k]))
        except jev.JevError as e:
            flagged.append(("-", f"Jev unavailable, only deterministic checks ran: {str(e)[:100]}", 0))
    lines = ["# Docs drift", "", f"Checked {len(paras)} paragraph(s) in {len(docs)} doc(s) that mention changed names.",
             ""]
    lines += [f"- {k} — {why}" + (f" (p={p:.2f})" if p and p < 1 else "") for k, why, p in flagged] or \
        ["- nothing looks stale"]
    out.write_text("\n".join(lines) + "\n")
    real = [f for f in flagged if f[0] != "-"]
    return f"{len(real)} paragraph(s) may be stale" if real else "nothing looks stale"


# ---- job ------------------------------------------------------------------------------------------
def run_job(d):
    st = jobs.read_status(d)
    rel = json.loads(Path(st["relevance"]).read_text())
    d = Path(d)
    results = {}
    for name, fn in (("test-gap", lambda: test_gap(rel, d / "test-gap.md")),
                     ("docs-drift", lambda: docs_drift(rel, d / "docs-drift.md", st.get("commit"), st.get("base"))),
                     ("review", lambda: adversarial(rel, st["relevance_md"], st["focus"], st.get("commit"),
                                                    st.get("base"), d / "review.md")[0])):
        t0 = time.time()
        try:
            results[name] = fn()
        except Exception as e:  # one failed check never stops the others
            results[name] = f"failed: {type(e).__name__}: {str(e)[:200]}"
        jobs.write_status(d, **{f"{name}_s": round(time.time() - t0)})
        _summary(d, rel, st, results)
    jobs.write_status(d, state="done", results=results)
    with open(LOG, "a") as fh:
        fh.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "job": d.name, "repo": rel["repo"],
                             "scope": rel["scope"], "results": results}) + "\n")


def _summary(d, rel, st, results):
    files = {"review": "review.md", "test-gap": "test-gap.md", "docs-drift": "docs-drift.md"}
    lines = [f"# Background review: {Path(rel['repo']).name} ({rel['scope']})", f"Focus: {st['focus']}",
             f"Relevance pass: {st['relevance_md']}", ""]
    for k in ("review", "test-gap", "docs-drift"):
        lines.append(f"- {k}: {results.get(k, 'running')} — {d / files[k]}")
    (d / "SUMMARY.md").write_text("\n".join(lines) + "\n")


def start(repo, focus, commit=None, base=None, background=True):
    rel, rel_path = relevance.build(repo, commit, base, focus)
    jid, d = jobs.new("review", focus or rel["scope"])
    jobs.write_status(d, kind="review", repo=rel["repo"], focus=focus or "", commit=commit, base=base,
                      relevance=str(rel_path), relevance_md=str(rel_path.with_suffix(".md")), state="queued")
    _summary(d, rel, jobs.read_status(d), {})
    if background:
        jobs.spawn(d, ["review", "--run-job", str(d)])
    else:
        jobs.write_status(d, state="running", pid=os.getpid())
        run_job(d)
    return jid, d


def status(jid=None):
    rows = jobs.listing("review", limit=50 if jid else 8)
    for d, st in rows:
        if jid and jid not in d.name:
            continue
        res = st.get("results") or {}
        print(f"{d.name}  {st.get('state')}  {d / 'SUMMARY.md'}")
        for k, v in res.items():
            print(f"    {k}: {v}")
    if not rows:
        print("no background reviews yet")


def main(args):
    from .cli import _flag, _opt
    if _flag(args, "--status"):
        return status(args[0] if args else None)
    run = _opt(args, "--run-job")
    if run:
        return run_job(run)
    repo = _opt(args, "--repo", ".")
    commit, base = _opt(args, "--commit"), _opt(args, "--base")
    wait = _flag(args, "--wait")
    _flag(args, "--background")
    focus = " ".join(args).strip() or "general review of this change"
    jid, d = start(repo, focus, commit, base, background=not wait)
    if wait:
        sys.stdout.write((d / "SUMMARY.md").read_text())
    else:
        print(f"gutcheck review: started in the background ({jid}); it never blocks. "
              f"Results land in {d}/SUMMARY.md (check with `gutcheck review --status {jid}`).")
