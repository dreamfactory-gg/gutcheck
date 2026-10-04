"""`gutcheck init` builds the personal config from this user's own history; `gutcheck refresh` updates it.

Both run as local code (no Claude tokens). Refresh never loosens thresholds; it only
  - records read-only command frequencies (for the report; read-only-ness is decided by the rules),
  - adds approved patterns: same normalised command asked about and approved 3 times, with no
    destructive, secret-printing or hidden-command finding,
  - adds ask patterns for probable misses: a command followed by the owner saying stop/undo/revert.
"""
import json
import re
import time
from collections import Counter

from gutcheck_core import config, secrets
from gutcheck_core.health import _stamp, _touch

MISS_RE = re.compile(r"(?i)^\s*(stop\b|undo\b|revert\b|why did you|you (deleted|killed|broke|restarted|removed)|"
                     r"roll ?back)")


def mine(since=0.0):
    """Scan transcripts newer than `since`. Returns stats and candidate patterns."""
    from gutcheck_core import transcripts
    try:
        from gutcheck_core import commands
    except Exception:  # tree-sitter missing: skip command classification
        commands = None
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
            if commands is not None:
                try:
                    an = commands.analyze(cmd, r.get("cwd"))
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
    from gutcheck_core.commands import normalize
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


def run(kind, quiet=False):
    from gutcheck_core import jev
    user = config.user_config()
    since = 0 if kind == "init" else _stamp("refresh")
    stats = mine(since)
    user.setdefault("keys", {"typesafe_service": "typesafe", "gateway_service": "ai-gateway"})
    bc = user.setdefault("bash_check", {})
    bc.setdefault("mode", "log")
    # misses -> ask patterns (tightening is always safe)
    from gutcheck_core.commands import normalize
    asks = set(bc.get("ask_patterns", []))
    for cmd in stats["misses"]:
        pat = "^" + re.escape(normalize(cmd)[:200])
        asks.add(pat)
    bc["ask_patterns"] = sorted(asks)[:500]
    if kind == "refresh":
        bc["approved_patterns"] = sorted(set(bc.get("approved_patterns", [])) | set(_learn_approvals(config.load())))
        promoted = _maybe_promote(bc)
        if promoted and not quiet:
            print(f"  bash check switched on: {promoted}")
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
        print(f"  config: {config.CONFIG_PATH}")


def main(kind, args):
    if kind == "init" and "--quiet" not in args and not secrets.get("typesafe") and not secrets.get("ai-gateway"):
        print("No TypeSafe or AI Gateway key found. Store one (you'll be prompted for the value):")
        print("  " + secrets.store_hint("typesafe"))
    run(kind, quiet="--quiet" in args)
