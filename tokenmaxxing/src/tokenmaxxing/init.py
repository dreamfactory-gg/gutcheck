"""`tokenmaxxing init` sets up the personal config (MCP adapters for the configured servers) and builds the skill,
doc/memory and recall indexes and the MCP cost table; `tokenmaxxing refresh` updates them and switches doc/memory
loading and the long-run stop check on once their logs show they're ready. Local code, except the MCP cost probes.
"""
import json
import time

from gutcheck_core import config, secrets
from gutcheck_core.health import _touch


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


def _measure_mcp(kind):
    """Per-request cost of each MCP server: at init, and at refresh when the configured servers changed."""
    from . import mcpload
    servers = set(mcpload.user_servers())   # empty on a failed read of ~/.claude.json: don't re-measure then
    if servers and (kind == "init" or set(mcpload._read(mcpload.LOAD, {}).get("servers", {})) != servers):
        try:
            mcpload.measure()
        except Exception as e:
            (config.STATE_DIR / "background.log").open("a").write(f"{kind}: MCP load measure failed: {e}\n")


def run(kind, quiet=False):
    from gutcheck_core import jev
    user = config.user_config()
    user.setdefault("keys", dict(config.CORE_DEFAULTS["keys"]))
    if "adapters" not in user:  # MCP adapters for servers configured here; later edits by the user are kept
        try:
            from . import adapters
            user["adapters"] = adapters.detect()
        except Exception:
            user["adapters"] = []
    if kind == "refresh":
        try:
            ctx_on = _maybe_promote_context(user)
            if ctx_on and not quiet:
                print(f"  doc/memory loading switched on: {ctx_on}")
        except Exception:
            pass
    config.save(user)
    new_skills, ctx_files = _index_context()
    hist, hist_s = _index_history()
    _touch("refresh")
    _measure_mcp(kind)
    from . import mcpload
    use = None
    try:
        use = mcpload.usage()
        mcpload.record_use(use[0])
    except Exception as e:
        (config.STATE_DIR / "background.log").open("a").write(f"{kind}: MCP use count failed: {e}\n")
    if not quiet:
        print(f"tokenmaxxing {kind}:")
        print(f"  key: {'found' if jev.have_key() else 'MISSING - ' + secrets.store_hint('typesafe')}")
        print(f"  skills newly indexed: {', '.join(new_skills) or 'none'}; doc/memory files (re)indexed: {ctx_files}")
        if hist is not None:
            print(f"  recall index: re-read {hist['claude']} transcripts, {hist['memory']} memory files, "
                  f"{hist['codex']} Codex runs, checked {hist['git']} git repos ({hist_s:.0f}s)")
        print(f"  config: {config.CONFIG_PATH}")
        mcpload.print_table(mcpload.table(use=use))   # the probes ran above

