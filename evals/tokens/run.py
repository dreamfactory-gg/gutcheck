"""Token eval + ratchet for gutcheck releases.

Small headless tasks run with `claude -p --output-format json` against a CANDIDATE copy of the plugin
(--plugin-dir), with the installed plugins, user settings, CLAUDE.md files and auto-memory switched off and
gutcheck's state, config, plugin data and ~/.claude reads pointed at temp dirs. Each task runs N times; the
median of each metric is compared with a ceiling file (default evals/tokens/ceilings.json). The maintainers'
ceilings are measured on their machine and kept out of this repo; build your own baseline with `run.py bootstrap`.

Metrics (summed over every model the run used, from the result JSON's modelUsage):
  input       input_tokens + cache_creation_input_tokens   (uncached prompt tokens, cache writes included)
  cache_read  cache_read_input_tokens
  output      output_tokens (thinking included)

  run.py measure  --plugin DIR [--runs N] [--tasks a,b] [--out FILE]   run the suite, write results
  run.py gate     --results FILE [--ceilings FILE]                     table; exit 1 on a failed check or ceiling
  run.py lower    --results FILE --ceilings FILE --sha SHA             ceiling = min(old, median * (1 + tol))
  run.py raise    --results FILE --ceilings FILE --sha SHA --task T --reason TEXT   (maintainers, via the release script)
  run.py bootstrap --results FILE [--results FILE ...] --ceilings FILE --sha SHA [--spread-results FILE ...] [--tolerance auto|in,cr,out]
  run.py spread   --results FILE [--results FILE ...]                 run-to-run spread, for choosing the tolerance
  run.py summary  --results FILE                                      one line: runs, time, cost
"""
import argparse
import itertools
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
CEILINGS = HERE / "ceilings.json"
METRICS = ("input", "cache_read", "output")
MODEL = "sonnet"     # an alias, never a version
EFFORT = "low"
MAX_TURNS = "30"
RUN_TIMEOUT = 300           # a run normally takes 5-15 s
RELEASE_RUNS = int(os.environ.get("GUTCHECK_TOKEN_RUNS", 3))   # runs per task in a release (the median counts)
TOOLS = "Bash,Read,Edit,Grep,Glob,Skill"   # Skill: gutcheck's own skills are listed to the model
MCP_TOOLS = "mcp__ci-fixture__fetch_ci_logs,mcp__ci-fixture__list_orders"


# ---- checks ---------------------------------------------------------------------------------------------------
def check_log(res, wd):
    want = {"test_rounding_half_even", "test_zone_lookup_unknown", "test_token_refresh_race"}
    got = set(re.findall(r"test_\w+", res))
    files = {"test_invoice", "test_shipping", "test_auth"}   # file stems are fine if it names them too
    if want <= got and not (got - want - files):
        return True, ""
    return False, f"named {sorted(got)}"


def check_source(res, wd):
    nums = re.findall(r"\d[\d,]*", res)
    return ("1125" in [n.replace(",", "") for n in nums]), f"answer {res.strip()[:80]!r}"


def check_edit(res, wd):
    t = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=wd, capture_output=True, text=True, timeout=60)
    if t.returncode != 0:
        return False, "tests still fail"
    d = subprocess.run(["git", "diff", "--numstat"], cwd=wd, capture_output=True, text=True).stdout.split()
    if d != ["1", "1", "shop/pricing.py"]:
        return False, f"diff was {' '.join(d)!r}, want one line in shop/pricing.py"
    return True, ""


def check_mcp_log(res, wd):
    ok = "deploy-canary" in res and "HC-7731" in res
    return ok, "" if ok else f"answer {res.strip()[:80]!r}"


def _stuck_id():
    sys.path.insert(0, str(FIX / "mcp"))
    import server
    d = json.loads(server.orders("north"))
    return next(o["id"] for o in d["orders"] if o["status"] == "stuck")


def check_mcp_json(res, wd):
    want = _stuck_id()
    return want in res, "" if want in res else f"answer {res.strip()[:80]!r}, want {want}"


TASKS = {
    "log_triage": dict(
        fixture="ci_log", check=check_log,
        prompt="build.log in the current directory is the output of a CI test run. Which tests failed? "
               "Reply with only the failing test function names, one per line, nothing else."),
    "big_source": dict(
        fixture="big_source", check=check_source,
        prompt="In ledger.py, what does late_fee(25000, 20) return? Reply with only the integer."),
    "one_line_edit": dict(
        fixture="small_repo", git=True, check=check_edit,
        prompt="The unit tests in this repo fail (run them with `python3 -m unittest -q`). Fix the bug in the "
               "source with a one-line change; don't edit the tests. Reply with one short sentence when done."),
    "mcp_log": dict(
        mcp=True, check=check_mcp_log,
        prompt="Use the ci-fixture MCP tool fetch_ci_logs for run 4411. Which job failed, and what is its error "
               "code? Reply exactly as `job: code`."),
    "mcp_json": dict(
        mcp=True, check=check_mcp_json,
        prompt="Use the ci-fixture MCP tool list_orders for region \"north\". Which order has status \"stuck\"? "
               "Reply with only its id."),
}


# untimed runs before round 1 so every measured run reads the same warm prompt cache (with and without MCP tools)
WARMUPS = {"_warm": dict(prompt="Reply with the single word: ok", check=lambda res, wd: (True, "")),
           "_warm_mcp": dict(prompt="Reply with the single word: ok", mcp=True, check=lambda res, wd: (True, ""))}


# ---- environment ------------------------------------------------------------------------------------------------
def clean_env():
    """The caller's environment minus anything Claude Code or gutcheck set for the current session."""
    keep = {"CLAUDE_CONFIG_DIR", "GUTCHECK_HOME"}
    return {k: v for k, v in os.environ.items()
            if k in keep or not (k.startswith("CLAUDE") or k.startswith("GUTCHECK_") or k.startswith("ANTHROPIC_MODEL"))}


def prepare(root, plugin):
    """Shared, read-only inputs for every run: a fake ~/.claude with made-up skills, the skill index built from
    it, a config file (engine defaults, so `init` never runs) and fresh refresh stamps."""
    base = root / "base"
    claude_dir = root / "claude"
    shutil.copytree(FIX / "skills", claude_dir / "skills")
    for sub in ("state", "config", "data"):
        (base / sub).mkdir(parents=True)
    (base / "config" / "config.toml").write_text("# token eval: engine defaults\n")
    for stamp in ("refresh", "mcpwaste"):
        (base / "state" / f"{stamp}.stamp").write_text("")
    env = dict(clean_env(), **iso_env(base, claude_dir, root), PYTHONPATH=str(plugin / "src"))
    subprocess.run([sys.executable, "-m", "gutcheck.cli", "skill-find", "--reindex"], env=env, check=True,
                   capture_output=True, text=True, timeout=120)
    if not (base / "data" / "skills.json").exists():
        sys.exit("token eval: skill index was not built")
    return base, claude_dir


def iso_env(d, claude_dir, root):
    empty = root / "empty"
    empty.mkdir(exist_ok=True)
    return {"GUTCHECK_STATE_DIR": str(d / "state"), "GUTCHECK_CONFIG_DIR": str(d / "config"),
            "GUTCHECK_DATA_DIR": str(d / "data"), "GUTCHECK_CLAUDE_DIR": str(claude_dir),
            "GUTCHECK_PROJECTS_DIR": str(empty), "GUTCHECK_CODEX_DIR": str(empty), "GUTCHECK_GIT_ROOT": str(empty)}


def run_one(task, i, root, base, claude_dir, plugin, model):
    spec = TASKS.get(task) or WARMUPS[task]
    d = root / f"{task}-{i}"
    shutil.copytree(base, d)
    wd = d / "work"
    if spec.get("fixture"):
        shutil.copytree(FIX / spec["fixture"], wd, ignore=shutil.ignore_patterns("__pycache__"))
    else:
        wd.mkdir()
    if spec.get("git"):
        g = ["git", "-c", "user.name=eval", "-c", "user.email=eval@example.invalid"]
        subprocess.run(["git", "init", "-q"], cwd=wd, check=True)
        subprocess.run(g + ["add", "-A"], cwd=wd, check=True)
        subprocess.run(g + ["commit", "-qm", "fixture"], cwd=wd, check=True)
    allowed = TOOLS
    cmd = ["claude", "-p", spec["prompt"], "--output-format", "json", "--model", model, "--effort", EFFORT,
           "--plugin-dir", str(plugin), "--setting-sources", "", "--strict-mcp-config",
           "--no-session-persistence", "--permission-mode", "dontAsk", "--tools", TOOLS,
           "--max-turns", MAX_TURNS, "--add-dir", str(d / "data")]
    if spec.get("mcp"):
        cfg = d / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"ci-fixture": {
            "type": "stdio", "command": sys.executable, "args": [str(FIX / "mcp" / "server.py")]}}}))
        cmd += ["--mcp-config", str(cfg)]
        allowed += "," + MCP_TOOLS
    cmd += ["--allowedTools", allowed]
    env = dict(clean_env(), **iso_env(d, claude_dir, root), CLAUDE_CODE_DISABLE_CLAUDE_MDS="1",
               CLAUDE_CODE_DISABLE_AUTO_MEMORY="1", CLAUDE_CODE_DISABLE_BUNDLED_SKILLS="1",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    t0 = time.monotonic()
    rec = {"task": task, "run": i}
    try:
        p = subprocess.run(cmd, cwd=wd, env=env, capture_output=True, text=True, timeout=RUN_TIMEOUT,
                           stdin=subprocess.DEVNULL)
        out = json.loads(p.stdout)
    except subprocess.TimeoutExpired:
        out = {"is_error": True, "result": "", "error": f"timeout after {RUN_TIMEOUT}s"}
    except ValueError:
        out = {"is_error": True, "result": "", "error": f"no JSON result (exit {p.returncode}): {p.stderr[-300:]}"}
    rec["seconds"] = round(time.monotonic() - t0, 1)
    mu = out.get("modelUsage") or {}
    rec["models"] = sorted(mu)
    rec["usage"] = {
        "input": sum((m.get("inputTokens") or 0) + (m.get("cacheCreationInputTokens") or 0) for m in mu.values()),
        "cache_read": sum(m.get("cacheReadInputTokens") or 0 for m in mu.values()),
        "output": sum(m.get("outputTokens") or 0 for m in mu.values())}
    rec["cost_usd"] = out.get("total_cost_usd")
    rec["turns"] = out.get("num_turns")
    res = out.get("result") or ""
    if out.get("is_error") or out.get("subtype") not in (None, "success"):
        rec["ok"], rec["why"] = False, out.get("error") or f"run ended: {out.get('subtype')}"
    else:
        try:
            rec["ok"], rec["why"] = spec["check"](res, wd)
        except Exception as e:
            rec["ok"], rec["why"] = False, f"check crashed: {type(e).__name__}: {e}"
    # harness guards: the candidate's hooks fired exactly once per prompt (so no second gutcheck is loaded), and
    # for MCP tasks the whole tool output reached gutcheck's MCP layer (not Claude Code's own large-output stub)
    rec["prompt_hooks"] = _lines(d / "state" / "prompt_hook.jsonl")
    if len(rec["prompt_hooks"]) != 1:
        rec["ok"], rec["why"] = False, (f"the candidate's UserPromptSubmit hook ran {len(rec['prompt_hooks'])} "
                                        "times (want 1: not loaded, or loaded twice)")
    if spec.get("mcp") and task in TASKS:
        seen = [x.get("chars", 0) for x in _lines(d / "state" / "mcp_trim.jsonl")]
        rec["mcp_chars"] = seen
        if not any(c >= 40_000 for c in seen):
            rec["ok"], rec["why"] = False, f"gutcheck's MCP layer never saw the full tool output ({seen})"
    rec["result"] = res[:300]
    return rec


def _lines(p):
    try:
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def claude_version():
    try:
        return subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30).stdout.split()[0]
    except Exception:
        return "?"


def measure(a):
    plugin = Path(a.plugin).resolve()
    if not (plugin / ".claude-plugin" / "plugin.json").exists():
        sys.exit(f"token eval: {plugin} is not a plugin dir")
    tasks = a.tasks.split(",") if a.tasks else list(TASKS)
    root = Path(tempfile.mkdtemp(prefix="gutcheck-tokens."))
    t0 = time.monotonic()
    try:
        base, claude_dir = prepare(root, plugin)
        needs = {"_warm"} | ({"_warm_mcp"} if any(TASKS[t].get("mcp") for t in tasks) else set())
        with ThreadPoolExecutor(max_workers=2) as ex:
            warm = list(ex.map(lambda w: run_one(w, 0, root, base, claude_dir, plugin, a.model), sorted(needs)))
        bad = [w for w in warm if not w["ok"]]
        if bad:
            sys.exit(f"token eval: warm-up run failed: {bad[0]['why']}")
        warm_cost = sum(w["cost_usd"] or 0 for w in warm)
        runs = []
        for i in range(a.runs):   # rounds: every task once per round, tasks in parallel within a round
            order = tasks[:]
            random.Random(i).shuffle(order)
            with ThreadPoolExecutor(max_workers=a.jobs) as ex:
                futs = [ex.submit(run_one, t, i, root, base, claude_dir, plugin, a.model) for t in order]
                for f in futs:
                    r = f.result()
                    runs.append(r)
                    if not a.quiet:
                        u = r["usage"]
                        print(f"    {r['task']:<14} run {i + 1}: in {u['input']:>7,}  cache_read {u['cache_read']:>8,}  "
                              f"out {u['output']:>6,}  {r['seconds']:>5.0f}s  {'ok' if r['ok'] else 'FAIL ' + r['why']}",
                              flush=True)
    finally:
        if a.keep:
            print(f"kept run dirs in {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    out = {"model": a.model, "effort": EFFORT, "runs": a.runs, "claude_code": claude_version(),
           "seconds": round(time.monotonic() - t0),
           "cost_usd": round(warm_cost + sum(r["cost_usd"] or 0 for r in runs), 3),
           "results": runs, "medians": medians(runs)}
    Path(a.out).write_text(json.dumps(out, indent=1))
    if not a.quiet:
        print(f"  token suite: {len(runs)} runs in {out['seconds']}s, ${out['cost_usd']:.2f}")
    return out


def medians(runs):
    by = {}
    for r in runs:
        by.setdefault(r["task"], []).append(r)
    return {t: {m: int(statistics.median(x["usage"][m] for x in rs)) for m in METRICS}
            | {"checks": f"{sum(x['ok'] for x in rs)}/{len(rs)}", "ok": all(x["ok"] for x in rs)}
            for t, rs in by.items()}


# ---- ceilings ---------------------------------------------------------------------------------------------------
def load(p):
    return json.loads(Path(p).read_text())


def save(p, d):
    Path(p).write_text(json.dumps(d, indent=1) + "\n")


def k(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def gate(a):
    res, ceil = load(a.results), load(a.ceilings)
    med = res["medians"]
    fail = []
    print(f"  {'task':<14} {'input (median/ceiling)':>24} {'cache_read':>18} {'output':>14}  checks  result")
    for t in sorted(set(med) | set(ceil["tasks"])):
        m, c = med.get(t), ceil["tasks"].get(t)
        if m is None:
            print(f"  {t:<14} not run")
            continue
        bad = [x for x in METRICS if c and m[x] > c[x]]
        why = []
        if not m["ok"]:
            why.append("check failed")
        if c is None:
            why.append("no ceiling")
        if bad:
            why.append("over: " + ",".join(bad))
        cells = [f"{k(m[x])}/{k(c[x]) if c else '-'}" + ("!" if x in bad else "") for x in METRICS]
        print(f"  {t:<14} {cells[0]:>24} {cells[1]:>18} {cells[2]:>14}  {m['checks']:>6}  "
              f"{'pass' if not why else 'FAIL (' + '; '.join(why) + ')'}")
        if why:
            fail.append(t)
            for r in res["results"]:
                if r["task"] == t and not r["ok"]:
                    print(f"      run {r['run'] + 1}: {r['why']}")
    mw = ceil.get("measured_with", {})
    notes = []
    if mw.get("claude_code") and mw["claude_code"] != res.get("claude_code"):
        notes.append(f"Claude Code {mw['claude_code']} -> {res.get('claude_code')}")
    got_models = sorted({x for r in res["results"] for x in r["models"]})
    if mw.get("models") and got_models and mw["models"] != got_models:
        notes.append(f"models {mw['models']} -> {got_models}")
    if notes and fail:
        print("  note: ceilings were measured with a different setup (" + "; ".join(notes) + "); if the base "
              "prompt grew, re-baseline with the release script's --raise-ceiling TASK --reason ...")
    return 1 if fail else 0


def tol_for(ceil, task, metric):
    """Tolerance for one task and metric: {task: {metric: x}}, else a per-metric default {metric: x}."""
    t = ceil["tolerance"]
    per = t.get(task)
    return per.get(metric, 0.1) if isinstance(per, dict) else t.get(metric, 0.1)


def _hist(ceil, sha, task, metric, old, new, why):
    ceil.setdefault("history", []).append({"t": time.strftime("%Y-%m-%dT%H:%M"), "sha": sha, "task": task,
                                           "metric": metric, "from": old, "to": new, "why": why})


def lower(a):
    res, ceil = load(a.results), load(a.ceilings)
    changed = []
    for t, m in res["medians"].items():
        c = ceil["tasks"].get(t)
        if not c or not m["ok"]:
            continue
        for x in METRICS:
            new = min(c[x], int(m[x] * (1 + tol_for(ceil, t, x))))
            if new < c[x]:
                _hist(ceil, a.sha, t, x, c[x], new, "lowered by release")
                changed.append(f"{t}.{x} {c[x]:,}->{new:,}")
                c[x] = new
    if changed:
        save(a.ceilings, ceil)
    print("  ceilings lowered: " + (", ".join(changed) if changed else "none"))
    return 0


def raise_(a):
    res, ceil = load(a.results), load(a.ceilings)
    if not a.reason or len(a.reason.strip()) < 8:
        sys.exit("raise: give a real --reason")
    m = res["medians"].get(a.task)
    if not m:
        sys.exit(f"raise: {a.task} was not measured")
    if not m["ok"]:
        sys.exit(f"raise: {a.task} failed its check ({m['checks']}); fix that first")
    c = ceil["tasks"].setdefault(a.task, {x: 0 for x in METRICS})
    for x in METRICS:
        new = int(m[x] * (1 + tol_for(ceil, a.task, x)))
        if new > c[x]:
            _hist(ceil, a.sha, a.task, x, c[x], new, "raised by owner: " + a.reason.strip())
            print(f"  {a.task}.{x}: {c[x]:,} -> {new:,}")
            c[x] = new
    ceil["measured_with"] = setup_of(res)
    save(a.ceilings, ceil)
    return 0


def setup_of(res):
    return {"claude_code": res.get("claude_code"), "model": res.get("model"), "effort": res.get("effort"),
            "models": sorted({x for r in res["results"] for x in r["models"]})}


GRID = (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)


def false_fail_rate(values, tol, n=3, releases=20, trials=2000, seed=1):
    """Chance that an unchanged plugin fails the gate at least once in `releases` releases, drawing each run
    from `values` (the measured runs), with ceilings bootstrapped at median * (1 + tol) and lowered by the ratchet."""
    rng = random.Random(seed)
    boot = statistics.median(values) * (1 + tol)
    fails = 0
    for _ in range(trials):
        c = boot
        for _ in range(releases):
            m = statistics.median(rng.choice(values) for _ in range(n))
            if m > c:
                fails += 1
                break
            c = min(c, m * (1 + tol))
    return fails / trials


# Lowest tolerance per metric. Runs measured in one sitting agree more than runs hours apart: the first release
# after bootstrap saw one task's output median at 243 where 12 bootstrap runs had all been 358-365 (thinking length
# flips between two modes), so output never gets less than +50%.
FLOOR = {"input": 0.1, "cache_read": 0.1, "output": 0.5}


def choose_tolerance(values, n=3, target=0.05, floor=0.0):
    """Smallest grid tolerance (at least `floor`) with under `target` chance of any false failure in 20
    releases, one step up for the tails a dozen samples can't show."""
    grid = [g for g in GRID if g >= floor] or [GRID[-1]]
    for i, g in enumerate(grid):
        if false_fail_rate(values, g, n) < target:
            return grid[min(i + 1, len(grid) - 1)]
    return grid[-1]


def bootstrap(a):
    runs = [r for p in a.results for r in load(p)["results"]]
    first = load(a.results[0])
    med = medians(runs)
    bad = [t for t, m in med.items() if not m["ok"]]
    if bad:
        sys.exit(f"bootstrap: checks failed for {bad}")
    by = {}
    for r in runs:
        by.setdefault(r["task"], []).append(r)
    if a.tolerance == "auto":
        # spread from every sitting given (--results and --spread-results), each run scaled by its own sitting's
        # median so sittings of slightly different commits still pool
        rel = {}
        for p in a.results + (a.spread_results or []):
            grp = {}
            for r in load(p)["results"]:
                grp.setdefault(r["task"], []).append(r)
            for t, rs in grp.items():
                for x in METRICS:
                    mid = statistics.median(r["usage"][x] for r in rs) or 1
                    rel.setdefault((t, x), []).extend(r["usage"][x] / mid for r in rs)
        tol = {t: {x: choose_tolerance(rel[(t, x)], n=RELEASE_RUNS, floor=FLOOR[x]) for x in METRICS}
               for t in sorted(by)}
    else:
        tol = dict(zip(METRICS, (float(x) for x in a.tolerance.split(","))))
    ceil = {"about": "Per-task token ceilings for gutcheck releases (see evals/tokens/run.py). Medians must stay "
                     "at or below these; a release lowers them to min(old, median * (1 + tolerance)). Only the "
                     "maintainer raises them (release script: --raise-ceiling TASK --reason ...). Tolerances come from the "
                     "run-to-run spread measured at bootstrap (under 5% chance of any false failure in 20 "
                     "releases of an unchanged plugin, then one step up; at least +10% for input and cache_read and "
                     "+50% for output).",
            "tolerance": tol, "measured_with": setup_of(first), "tasks": {}, "history": []}
    for t, m in sorted(med.items()):
        ceil["tasks"][t] = {x: int(m[x] * (1 + tol_for(ceil, t, x))) for x in METRICS}
        for x in METRICS:
            _hist(ceil, a.sha, t, x, None, ceil["tasks"][t][x], f"bootstrap from {len(by[t])} runs")
    save(a.ceilings, ceil)
    print(json.dumps({"tolerance": tol, "tasks": ceil["tasks"]}, indent=1))
    return 0


def spread(a):
    """Per task and metric: spread of single runs, and how far a median of 3 moves (all 3-run subsets)."""
    runs = [r for p in a.results for r in load(p)["results"]]
    by = {}
    for r in runs:
        by.setdefault(r["task"], []).append(r)
    worst = {x: 0.0 for x in METRICS}
    for t, rs in sorted(by.items()):
        line = [f"  {t:<14} n={len(rs):<2}"]
        for x in METRICS:
            v = [r["usage"][x] for r in rs]
            med3 = [statistics.median(c) for c in itertools.combinations(v, 3)] if len(v) >= 3 else v
            ratio = max(med3) / max(1, min(med3)) - 1       # worst lucky-low vs unlucky-high median of 3
            worst[x] = max(worst[x], ratio)
            line.append(f"{x} {min(v):,}..{max(v):,} (med3 range +{ratio:.0%})")
        print("  ".join(line) + f"  checks {sum(r['ok'] for r in rs)}/{len(rs)}")
    print("  worst median-of-3 range: " + ", ".join(f"{x} +{worst[x]:.0%}" for x in METRICS))
    return 0


def summary(a):
    res = load(a.results)
    print(f"{len(res['results'])} runs of {len(res['medians'])} tasks, {res['seconds']}s, ${res['cost_usd']:.2f}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure")
    m.add_argument("--plugin", required=True)
    m.add_argument("--runs", type=int, default=RELEASE_RUNS)
    m.add_argument("--jobs", type=int, default=5)
    m.add_argument("--tasks")
    m.add_argument("--model", default=MODEL)
    m.add_argument("--out", default="tokens.results.json")
    m.add_argument("--keep", action="store_true")
    m.add_argument("--quiet", action="store_true")
    for name in ("gate", "lower", "raise", "bootstrap", "spread", "summary"):
        p = sub.add_parser(name)
        p.add_argument("--results", required=True, action="append" if name in ("bootstrap", "spread") else "store")
        p.add_argument("--ceilings", default=str(CEILINGS))
        p.add_argument("--sha", default="?")
        p.add_argument("--task")
        p.add_argument("--reason")
        p.add_argument("--tolerance", default="auto", help="auto (from the measured spread) or in,cr,out")
        p.add_argument("--spread-results", action="append", help="bootstrap: more sittings, for the spread only")
    a = ap.parse_args()
    if a.cmd == "measure":
        measure(a)
        return 0
    return {"gate": gate, "lower": lower, "raise": raise_, "bootstrap": bootstrap, "spread": spread,
            "summary": summary}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
