"""`gutcheck init` builds the personal config from this user's own history; `gutcheck refresh` updates it.

Both run as local code (no Claude tokens). Refresh never loosens thresholds; it only
  - records read-only command frequencies (for the report; read-only-ness is decided by the rules),
  - adds approved patterns: same normalised command asked about and approved 3 times, with no
    destructive, secret-printing or hidden-command finding,
  - adds ask patterns for probable misses: a command followed by the owner saying stop/undo/revert.
"""
import json
import os
import re
import time
from collections import Counter

from . import config, secrets

MISS_RE = re.compile(r"(?i)^\s*(stop\b|undo\b|revert\b|why did you|you (deleted|killed|broke|restarted|removed)|"
                     r"roll ?back)")


def _stamp(name):
    p = config.STATE_DIR / f"{name}.stamp"
    return p.stat().st_mtime if p.exists() else 0


def _touch(name):
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (config.STATE_DIR / f"{name}.stamp").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))


def mine(since=0.0):
    """Scan transcripts newer than `since`. Returns stats and candidate patterns."""
    from . import transcripts
    try:
        from . import bashcheck
    except Exception:  # tree-sitter missing: skip command classification
        bashcheck = None
    heads, misses, hosts, n = Counter(), [], Counter(), 0
    for f in transcripts.files(since=since):
        for r in transcripts.bash_commands(f):
            n += 1
            cmd = r["cmd"]
            m = re.findall(r"\bssh\s+(?:-\S+\s+(?:\S+\s+)?)*([\w.\-]+@)?([\w.\-]+)", cmd)
            for _, h in m:
                hosts[h] += 1
            if r.get("next_prompt") and MISS_RE.search(r["next_prompt"]) and r["outcome"] == "ok":
                misses.append(cmd)
            if bashcheck is not None:
                try:
                    an = bashcheck.analyze(cmd, r.get("cwd"))
                    if an.readonly:
                        for c in an.cmds:
                            heads[c.name] += 1
                except Exception:
                    pass
    return {"commands": n, "readonly_heads": heads, "misses": misses, "ssh_hosts": hosts}


def _learn_approvals(cfg):
    """Patterns asked about and approved >= 3 times with no hard finding -> approved_patterns."""
    from . import report
    checks = report._read("bashcheck")
    ran = {d["tool_use_id"] for d in report._read("bash_ran") if d.get("tool_use_id")}
    from .bashcheck import normalize
    counts = Counter()
    for d in checks:
        if d.get("mode") == "on" and d.get("decision") == "ask" and d.get("tool_use_id") in ran and \
                d.get("stage") in ("jev", "trigger") and d.get("check") not in ("secret", "hidden", "runtime",
                                                                               "git-history", "git-discard"):
            counts[normalize(d["cmd"])] += 1
    approved = set(cfg["bash_check"].get("approved_patterns", []))
    new = [p for p, k in counts.items() if k >= 3 and p not in approved]
    return new


def _maybe_promote(bc):
    """Logging-only -> on, once live logging is healthy. Accuracy was gated by the replay eval
    (evals/bash_eval.py); this gate checks the live hook: enough checks, few errors, fast, rarely failing open.
    Set bash_check.auto_promote = false to keep it in logging mode."""
    from . import report
    if bc.get("mode", "log") != "log" or bc.get("auto_promote") is False:
        return None
    checks = report._read("bashcheck")
    if len(checks) < 100:
        return None
    errs = report._read("hook_errors")
    ms = sorted(d.get("ms") or 0 for d in checks)
    p95 = ms[int(len(ms) * 0.95)]
    failed = sum(d.get("stage") == "jev-failed-open" for d in checks)
    asks = sum(d.get("decision") == "ask" for d in checks)
    if len(errs) / len(checks) < 0.02 and p95 < 3000 and failed / len(checks) < 0.05 and asks / len(checks) < 0.05:
        bc["mode"] = "on"
        report_line = f"{len(checks)} checks logged, p95 {p95} ms, {asks} would-be asks, {failed} fail-opens"
        (config.STATE_DIR / "promotions.log").open("a").write(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')} bash_check on: {report_line}\n")
        return report_line
    return None


def _index_context():
    """Skill index, project doc/memory index, and old per-session router state (Phase 6)."""
    new, n = [], 0
    try:
        from . import ctxload, router, skills
        _, new = skills.build()
        n = ctxload.build()
        router.prune_seen()
    except Exception as e:
        (config.STATE_DIR / "background.log").open("a").write(f"refresh: context index failed: {e}\n")
    return new, n


def _index_history():
    """Recall's tree index of past work (transcripts, memory incl. archive, Codex, git). Incremental: a daily
    refresh re-reads only what changed (seconds); the first build takes about a minute."""
    try:
        from . import index
        t0 = time.time()
        n = index.build()
        return n, time.time() - t0
    except Exception as e:
        (config.STATE_DIR / "background.log").open("a").write(f"refresh: recall index failed: {e}\n")
        return None, 0


def _maybe_promote_context(user, days=3, min_runs=30):
    """Doc/memory loading: log -> on once its logged background runs are fast (p95 < 1 s) and rarely fail.
    Picks were already checked against the previous version (evals/ctxload_eval.py); only speed was blocking."""
    ctx = user.setdefault("context", {})
    if ctx.get("mode", "log") != "log" or ctx.get("auto_promote") is False:
        return None
    since = time.time() - days * 86400
    runs = []
    p = config.STATE_DIR / "router.jsonl"
    if not p.exists():
        return None
    for line in p.open():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not d.get("background"):
            continue
        try:
            if time.mktime(time.strptime(d["t"], "%Y-%m-%dT%H:%M:%S")) < since:
                continue
        except (KeyError, ValueError):
            continue
        runs.append(d)
    if len(runs) < min_runs:
        return None
    ms = sorted(r.get("ms") or 0 for r in runs)
    p95 = ms[int(len(ms) * 0.95)]
    errors = sum(1 for r in runs if "error" in (r.get("context") or {}))
    if p95 < 1000 and errors / len(runs) < 0.1:
        ctx["mode"] = "on"
        line = f"{len(runs)} background runs, p95 {p95} ms, {errors} errors"
        (config.STATE_DIR / "promotions.log").open("a").write(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')} context loading on: {line}\n")
        return line
    return None


def run(kind, quiet=False):
    from . import jev
    user = config.user_config()
    since = 0 if kind == "init" else _stamp("refresh")
    stats = mine(since)
    user.setdefault("keys", {"typesafe_service": "typesafe", "gateway_service": "ai-gateway"})
    if "adapters" not in user:  # MCP adapters for servers configured here; later edits by the user are kept
        try:
            from . import adapters
            user["adapters"] = adapters.detect()
        except Exception:
            user["adapters"] = []
    bc = user.setdefault("bash_check", {})
    bc.setdefault("mode", "log")
    # misses -> ask patterns (tightening is always safe)
    from .bashcheck import normalize
    asks = set(bc.get("ask_patterns", []))
    for cmd in stats["misses"]:
        pat = "^" + re.escape(normalize(cmd)[:200])
        asks.add(pat)
    bc["ask_patterns"] = sorted(asks)[:500]
    if kind == "refresh":
        bc["approved_patterns"] = sorted(set(bc.get("approved_patterns", [])) | set(_learn_approvals(config.load())))
        promoted = _maybe_promote(bc)
        try:
            ctx_on = _maybe_promote_context(user)
            if ctx_on and not quiet:
                print(f"  doc/memory loading switched on: {ctx_on}")
        except Exception:
            pass
        if promoted and not quiet:
            print(f"  bash check switched on: {promoted}")
        try:  # long-run stop check: logging-only -> on once its logged TASKS.md blocks prove right (never loosens)
            from . import stopcheck
            stop_on = stopcheck.maybe_promote(user)
            if stop_on and not quiet:
                print(f"  {stop_on}")
        except Exception as e:
            (config.STATE_DIR / "background.log").open("a").write(f"refresh: stop check promotion failed: {e}\n")
    sens = user.setdefault("sensitivity", {})
    sens.setdefault("hosts", {})
    sens.setdefault("branches", {"main": "shared default branch", "master": "shared default branch"})
    sens.setdefault("projects", {})
    config.save(user)
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (config.STATE_DIR / "mined.json").write_text(json.dumps({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "commands": stats["commands"],
        "readonly_heads": stats["readonly_heads"].most_common(200),
        "ssh_hosts": stats["ssh_hosts"].most_common(50), "misses": len(stats["misses"])}, indent=1))
    new_skills, ctx_files = _index_context()
    hist, hist_s = _index_history()
    _touch("refresh")
    if not quiet:
        print(f"gutcheck {kind}: scanned {stats['commands']} Bash commands")
        print(f"  key: {'found' if jev.have_key() else 'MISSING - ' + secrets.store_hint('typesafe')}")
        print(f"  read-only command heads seen: {len(stats['readonly_heads'])} "
              f"(top: {', '.join(h for h, _ in stats['readonly_heads'].most_common(8))})")
        unknown = [h for h, _ in stats["ssh_hosts"].most_common(10) if h not in sens["hosts"]]
        if unknown:
            print(f"  ssh hosts not in your sensitivity map: {', '.join(unknown)}")
            print(f"    label them in {config.CONFIG_PATH} under [sensitivity.hosts]")
        print(f"  probable misses turned into ask patterns: {len(stats['misses'])}")
        print(f"  skills newly indexed: {', '.join(new_skills) or 'none'}; doc/memory files (re)indexed: {ctx_files}")
        if hist is not None:
            print(f"  recall index: re-read {hist['claude']} transcripts, {hist['memory']} memory files, "
                  f"{hist['codex']} Codex runs, checked {hist['git']} git repos ({hist_s:.0f}s)")
        print(f"  config: {config.CONFIG_PATH}")


def main(kind, args):
    if kind == "init" and "--quiet" not in args and not secrets.get("typesafe") and not secrets.get("ai-gateway"):
        print("No TypeSafe or AI Gateway key found. Store one (you'll be prompted for the value):")
        print("  " + secrets.store_hint("typesafe"))
    run(kind, quiet="--quiet" in args)
