"""Token eval + ratchet for tokenmaxxing releases.

Small headless tasks run with `claude -p --output-format json` against a CANDIDATE copy of the plugin
(--plugin-dir), with the installed plugins, user settings, CLAUDE.md files and auto-memory switched off and
tokenmaxxing's state, config, plugin data, the standing-rules store and ~/.claude reads pointed at temp dirs. Each task runs N times; the
median of each metric is compared with a ceiling file (default evals/tokens/ceilings.json). The maintainers'
ceilings are measured on their machine and kept out of this repo; build your own baseline with `run.py bootstrap`.

Usage (summed over every model the run used, from the result JSON's modelUsage):
  input        input_tokens
  cache_write  cache_creation_input_tokens
  cache_read   cache_read_input_tokens
  output       output_tokens (thinking included)
  cost         input + cache_write * 1.25 + cache_read * 0.1 + output * 5   (cost-equivalent tokens)
  cost_prod    the same at production prices (1h cache): cache_write * 2, cache_read * 0.05; reported, never gated
Only cost is gated: a Claude Code release can turn cache writes into reads, which moves the raw counts in opposite
directions while the price goes the other way.

  run.py measure  --plugin DIR [--runs N] [--tasks a,b] [--out FILE] [--config TOML]
                                     run the suite (gated tasks unless --tasks names others), write results
  run.py gate     --results FILE [--ceilings FILE]                     table; exit 1 on a failed check or cost ceiling
  run.py lower    --results FILE --ceilings FILE --sha SHA             lower a ceiling when the costliest run shows the higher level
  run.py raise    --results FILE --ceilings FILE --sha SHA --task T --reason TEXT   (maintainers, via the release script)
  run.py bootstrap --results FILE [--results FILE ...] --ceilings FILE --sha SHA [--spread-results FILE ...] [--tolerance auto|X]
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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
CEILINGS = HERE / "ceilings.json"
WEIGHTS = {"input": 1, "cache_write": 1.25, "cache_read": 0.1, "output": 5}
PROD_WEIGHTS = {"input": 1, "cache_write": 2, "cache_read": 0.05, "output": 5}
FIELDS = (*WEIGHTS, "cost", "cost_prod")
METRICS = ("cost",)   # gated
MODEL = "sonnet"     # an alias, never a version
EFFORT = "low"
MAX_TURNS = "30"
RUN_TIMEOUT = 300           # a run normally takes 5-15 s
RELEASE_RUNS = int(os.environ.get("GUTCHECK_TOKEN_RUNS", 3))   # runs per task in a release (the median counts)
TOOLS = "Bash,Read,Edit,Grep,Glob,Skill"   # Skill: tokenmaxxing's own skills are listed to the model
MCP_TOOLS = "mcp__ci-fixture__fetch_ci_logs,mcp__ci-fixture__list_orders"


# ---- checks ---------------------------------------------------------------------------------------------------
def check_log(res, wd):
    want = {"test_rounding_half_even", "test_zone_lookup_unknown", "test_token_refresh_race"}
    got = set(re.findall(r"test_\w+", res))
    files = {"test_invoice", "test_shipping", "test_auth"}   # file stems are fine if it names them too
    if want <= got and not (got - want - files):
        return True, ""
    return False, f"named {sorted(got)}"


def check_pin(res, wd):
    return ("4.17.2" in res), f"answer {res.strip()[:80]!r}"


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


def check_handback(res, wd):
    got = dict(re.findall(r"^\s*([123])\s*[:.)]\s*(.+?)\s*$", res, re.M))
    ok = (got.get("1", "").strip("`*").upper() == "BLOCK" and "store.py" in got.get("2", "")
          and re.sub(r"\D", "", got.get("3", "")) == "15")
    return ok, "" if ok else f"answer {res.strip()[:120]!r}"


def check_long_loop(res, wd):
    sys.path.insert(0, str(FIX))
    from make import LONG_LOOP_FACTS
    got = dict(re.findall(r"^\s*([123])\s*[:.)]\s*(.+?)\s*$", res, re.M))
    right = sum(f.lower() in got.get(str(i), "").lower() for i, f in enumerate(LONG_LOOP_FACTS, 1))
    return right == 3, f"{right}/3 facts" + ("" if right == 3 else f": {res.strip()[:120]!r}")


def check_prefix(res, wd):
    return "pong" in res.lower(), f"answer {res.strip()[:80]!r}"


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
    "bash_log": dict(
        fixture="ci_log", check=check_log,
        prompt="Run `grep -v 'PASSED\\|DEBUG' build.log` (that exact command, once) and read its output: which tests failed? "
               "Reply with only the failing test function names, one per line, nothing else."),
    "bash_lookup": dict(
        fixture="freeze", check=check_pin,
        prompt="Run `cat freeze.txt` (that exact command, once) and read its output: which version of quillstone is "
               "pinned? Reply with only the version."),
    "bash_lookup_5k": dict(
        fixture="freeze_small", check=check_pin,
        prompt="Run `cat freeze.txt` (that exact command, once) and read its output: which version of quillstone is "
               "pinned? Reply with only the version."),
    "big_read": dict(
        fixture="big_source", check=check_source, wrong_ok=1 / 3,
        prompt="Use the Read tool on ledger.py, then tell me what late_fee(25000, 20) returns. Reply with only the "
               "integer."),
    "big_source": dict(
        fixture="big_source", check=check_source, wrong_ok=1 / 3,   # low-effort sonnet sometimes misreads the math
        prompt="In ledger.py, what does late_fee(25000, 20) return? Reply with only the integer."),
    "one_line_edit": dict(
        fixture="small_repo", git=True, check=check_edit,
        prompt="The unit tests in this repo fail (run them with `python3 -m unittest -q`). Fix the bug in the "
               "source with a one-line change; don't edit the tests. Reply with one short sentence when done."),
    "handback_cap": dict(
        fixture="audit_repo", check=check_handback, tools="Agent", gate=False, min_report=5000,   # never gated
        prompt="Delegate an audit of this repo to one subagent with the Agent tool (subagent_type general-purpose) "
               "and this brief, word for word: 'Audit every .py file under ledgerkit/ for bugs and security problems. "
               "Organise the report by file, in path order, and begin each file's section with its line count (from "
               "`wc -l`). For each finding give file:line, severity (high/medium/low), a three-sentence explanation "
               "and a suggested fix with a short code snippet. End with an overall verdict: SHIP, or BLOCK if any "
               "finding is high severity.' "
               "Answer only from what the subagent reports (its report, and any file it points you to); don't open "
               "the repo's files yourself. 1. What is the verdict? 2. Which file has the SQL injection? 3. How many "
               "lines does ledgerkit/notify.py have, per the audit? Reply with exactly three lines: `1: SHIP or "
               "BLOCK`, `2: file path`, `3: number`."),
    # Lever 1 (compact earlier): run on and off, e.g. --config '[compact]\nmain_tokens = 60000' against
    # '[compact]\nmode = "off"'. Pass: 3/3 facts with compaction on, cost -25% and turns <= off + 2.
    "long_loop": dict(
        fixture="long_loop", check=check_long_loop, gate=False, max_turns="80", timeout=1200,
        prompts=["Run `python3 init.py` once (it can only run once) and keep what it prints: you will need those three "
                 "values at the very end. Then use the Read tool on modules/m01.py (the whole file) and reply with one "
                 "line: how many functions it defines."]
                + [f"Use the Read tool on modules/m{a:02d}.py and modules/m{a + 1:02d}.py (whole files, no offset or "
                   f"limit). Then run `python3 check.py {a}`, `python3 check.py {a + 1}` and `python3 check.py {a} "
                   f"{a + 1}`, one Bash call each, in that order. Reply with one line: the smallest `retries=` default "
                   f"in the two files." for a in range(2, 14, 2)]
                + ["Without using any tools, from memory: what did init.py print? Reply with exactly three lines: "
                   "`1: codename`, `2: build number`, `3: checksum`."]),
    "mcp_log": dict(
        mcp=True, mcp_floor=40_000, check=check_mcp_log,
        prompt="Use the ci-fixture MCP tool fetch_ci_logs for run 4411. Which job failed, and what is its error "
               "code? Reply exactly as `job: code`."),
    "mcp_json": dict(
        mcp=True, mcp_floor=40_000, check=check_mcp_json,
        prompt="Use the ci-fixture MCP tool list_orders for region \"north\". Which order has status \"stuck\"? "
               "Reply with only its id."),
    # Lever 3 (fixed prefix): run with '[prefix]\nmode = "on"' against '"off"'. Pass: the subagent's first-request
    # cache_creation (sub_first) drops by 30% or more.
    "prefix": dict(
        mcp=True, tools="Agent", check=check_prefix, gate=False,
        prompt="Use the Agent tool once (subagent_type general-purpose) with this prompt, word for word: 'Reply with "
               "the single word: pong'. Then reply with only the word the subagent replied."),
}


# untimed runs before round 1 so every measured run reads the same warm prompt cache (with and without MCP tools)
WARMUPS = {"_warm": dict(prompt="Reply with the single word: ok", check=lambda res, wd: (True, "")),
           "_warm_mcp": dict(prompt="Reply with the single word: ok", mcp=True, check=lambda res, wd: (True, "")),
           "_warm_agent": dict(prompt="Reply with the single word: ok", tools="Agent",
                               check=lambda res, wd: (True, ""))}


# ---- environment ------------------------------------------------------------------------------------------------
def clean_env():
    """The caller's environment minus anything Claude Code or the plugins set for the current session."""
    keep = {"CLAUDE_CONFIG_DIR", "GUTCHECK_HOME"}
    return {k: v for k, v in os.environ.items()
            if k in keep or not (k.startswith("CLAUDE") or k.startswith(("GUTCHECK_", "TOKENMAXXING_")) or k.startswith("ANTHROPIC_MODEL"))}


def prepare(root, plugin, config=""):
    """Shared, read-only inputs for every run: a fake ~/.claude with made-up skills, the skill index built from
    it, a config file (engine defaults, so `init` never runs) and fresh refresh stamps."""
    base = root / "base"
    claude_dir = root / "claude"
    shutil.copytree(FIX / "skills", claude_dir / "skills")
    for sub in ("state", "config", "data"):
        (base / sub).mkdir(parents=True)
    (base / "config" / "config.toml").write_text("# token eval: engine defaults\n" + config)
    for stamp in ("refresh", "mcpwaste"):
        (base / "state" / f"{stamp}.stamp").write_text("")
    env = dict(clean_env(), **iso_env(base, claude_dir, root), PYTHONPATH=str(plugin / "src"))
    subprocess.run([sys.executable, "-m", "tokenmaxxing.cli", "skill-find", "--reindex"], env=env, check=True,
                   capture_output=True, text=True, timeout=120)
    if not (base / "data" / "skills.json").exists():
        sys.exit("token eval: skill index was not built")
    return base, claude_dir


def iso_env(d, claude_dir, root):
    empty = root / "empty"
    empty.mkdir(exist_ok=True)
    return {"TOKENMAXXING_STATE_DIR": str(d / "state"), "TOKENMAXXING_CONFIG_DIR": str(d / "config"),
            "TOKENMAXXING_DATA_DIR": str(d / "data"), "GUTCHECK_RULES_DIR": str(d / "rules"),
            "GUTCHECK_CLAUDE_DIR": str(claude_dir),
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
    tools = ",".join(filter(None, (TOOLS, spec.get("tools"))))   # e.g. Agent, only for the task that needs it
    allowed = tools
    prompts = spec.get("prompts")
    io = (["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"] if prompts
          else [spec["prompt"], "--output-format", "json"])
    cmd = ["claude", "-p", *io, "--model", model, "--effort", EFFORT,
           "--plugin-dir", str(plugin), "--setting-sources", "", "--strict-mcp-config",
           "--permission-mode", "dontAsk", "--tools", tools,
           "--max-turns", spec.get("max_turns", MAX_TURNS), "--add-dir", str(d / "data")]
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
    timeout = spec.get("timeout", RUN_TIMEOUT)
    try:
        if prompts:
            out = converse(cmd, prompts, wd, env, timeout)
            rec |= {k: out[k] for k in ("compactions", "last_turns", "peak_context")}
        else:
            p = subprocess.run(cmd, cwd=wd, env=env, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
            out = json.loads(p.stdout)
    except subprocess.TimeoutExpired:
        out = {"is_error": True, "result": "", "error": f"timeout after {timeout}s"}
    except ValueError:
        out = {"is_error": True, "result": "", "error": f"no JSON result (exit {p.returncode}): {p.stderr[-300:]}"}
    rec["seconds"] = round(time.monotonic() - t0, 1)
    # the session is persisted (tokenmaxxing's hooks read its transcript, as in real use); drop it from ~/.claude after
    for t in Path.home().glob(f".claude/projects/*/{out.get('session_id') or '-'}.jsonl"):
        if spec.get("tools") == "Agent":
            rec["parent"] = _priced(_usage(t))
            subs = list(t.with_suffix("").glob("subagents/agent-*.jsonl"))
            rec["report_chars"] = max((len(_last_text(f)) for f in subs), default=0)
            rec["sub_first"] = next((_priced(_usage(f, first=True)) for f in subs), None)
        t.unlink(missing_ok=True)
        shutil.rmtree(t.with_suffix(""), ignore_errors=True)
    mu = out.get("modelUsage") or {}
    rec["models"] = sorted(mu)
    u = {f: sum(m.get(key) or 0 for m in mu.values()) for f, key in (
        ("input", "inputTokens"), ("cache_write", "cacheCreationInputTokens"),
        ("cache_read", "cacheReadInputTokens"), ("output", "outputTokens"))}
    rec["usage"] = _priced(u)
    rec["cost_usd"] = out.get("total_cost_usd")
    rec["turns"] = out.get("num_turns")
    res = out.get("result") or ""
    if out.get("is_error") or out.get("subtype") not in (None, "success"):
        rec["ok"], rec["why"] = False, out.get("error") or f"run ended: {out.get('subtype')}"
    else:
        try:
            rec["ok"], rec["why"] = spec["check"](res, wd)
            rec["wrong"] = not rec["ok"]   # a wrong answer, which a task's wrong_ok may tolerate; guards below reset it
        except Exception as e:
            rec["ok"], rec["why"] = False, f"check crashed: {type(e).__name__}: {e}"
    # harness guards: the candidate's hooks fired exactly once per prompt (so no second tokenmaxxing is loaded), and
    # for MCP tasks the whole tool output reached tokenmaxxing's MCP layer (not Claude Code's own large-output stub)
    rec["prompt_hooks"] = _lines(d / "state" / "prompt_hook.jsonl")
    want = len(prompts or [1])
    if len(rec["prompt_hooks"]) != want:
        rec["ok"], rec["wrong"], rec["why"] = False, False, (f"the candidate's UserPromptSubmit hook ran {len(rec['prompt_hooks'])} "
                                                      f"times (want {want}: not loaded, or loaded twice)")
    if prompts and rec["ok"] and rec["last_turns"] != 1:
        rec["ok"], rec["why"] = False, f"the last prompt took {rec['last_turns']} turns (tools used to answer it)"
    if prompts:
        rec["mod_compactions"] = _lines(d / "state" / "compact.jsonl")
        rec["checkpoint_injects"] = _lines(d / "state" / "checkpoint_inject.jsonl")
    if spec.get("min_report") and rec["ok"] and rec.get("report_chars", 0) < spec["min_report"]:
        rec["skip"] = f"the subagent's report was short ({rec.get('report_chars')} chars)"   # nothing to cap: no data
    if spec.get("mcp_floor"):
        seen = [x.get("chars", 0) for x in _lines(d / "state" / "mcp_trim.jsonl")]
        rec["mcp_chars"] = seen
        if not any(c >= spec["mcp_floor"] for c in seen):
            rec["ok"], rec["wrong"], rec["why"] = False, False, f"tokenmaxxing's MCP layer never saw the full tool output ({seen})"
    rec["result"] = res[:300]
    return rec


def converse(cmd, prompts, wd, env, timeout):
    """One session over several prompts (stream-json): each prompt is sent once the previous one's turn has ended.
    A compaction ends in a result of its own (num_turns 0), which sends nothing. Returns the last result (its usage
    is cumulative) with the last prompt's answer, num_turns summed over the prompts, the compactions counted and the
    largest context one request was answered over."""
    p = subprocess.Popen(cmd, cwd=wd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True)
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    todo, turns, compactions, out, answer, peak = list(prompts), 0, 0, {}, {}, 0

    def send():
        p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": todo.pop(0)}}) + "\n")
        p.stdin.flush()
    try:
        send()
        for line in p.stdout:
            d = json.loads(line)
            compactions += d.get("subtype") == "compact_boundary"
            if d["type"] == "assistant":
                u = d["message"].get("usage") or {}
                peak = max(peak, sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                              "cache_creation_input_tokens")))
            if d["type"] != "result":
                continue
            out = d
            if d.get("num_turns"):
                turns, answer = turns + d["num_turns"], d
                if d.get("is_error") or d.get("subtype") != "success" or not todo:
                    p.stdin.close()
                else:
                    send()
        p.wait()
    finally:
        timer.cancel()
    if p.returncode == -9:
        raise subprocess.TimeoutExpired(cmd, timeout)
    if todo:
        out = out | {"is_error": True, "error": f"session ended with {len(todo)} prompts unsent"}
    return out | {"result": answer.get("result"), "num_turns": turns, "last_turns": answer.get("num_turns"),
                  "compactions": compactions, "peak_context": peak}


def _priced(u):
    return u | {"cost": int(sum(u[f] * w for f, w in WEIGHTS.items())),
                "cost_prod": int(sum(u[f] * w for f, w in PROD_WEIGHTS.items()))}


def _usage(transcript, first=False):
    """One session's own usage from its transcript (the last record of each API message; subagents live in their
    own files): what the parent alone paid, apart from its subagents. first: its first request alone."""
    by = {}
    for d in _lines(transcript):
        m = d.get("message") if isinstance(d.get("message"), dict) else {}
        if d.get("type") == "assistant" and m.get("usage"):
            if first and by and (m.get("id") or d.get("uuid")) not in by:
                break
            by[m.get("id") or d.get("uuid")] = m["usage"]
    return {f: sum(u.get(key) or 0 for u in by.values()) for f, key in (
        ("input", "input_tokens"), ("cache_write", "cache_creation_input_tokens"),
        ("cache_read", "cache_read_input_tokens"), ("output", "output_tokens"))}


def _last_text(transcript):
    last = ""
    for d in _lines(transcript):
        c = (d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None
        if d.get("type") == "assistant" and isinstance(c, list):
            last = "\n".join(b.get("text", "") for b in c if b.get("type") == "text") or last
            last = next((b["input"].get("message", "") for b in c if b.get("type") == "tool_use"
                         and b.get("name") == "SubagentHandback"), last)
    return last


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
    tasks = a.tasks.split(",") if a.tasks else [t for t, spec in TASKS.items() if spec.get("gate", True)]
    root = Path(tempfile.mkdtemp(prefix="gutcheck-tokens."))
    t0 = time.monotonic()
    try:
        base, claude_dir = prepare(root, plugin, a.config.replace("\\n", "\n"))
        needs = ({"_warm"} | ({"_warm_mcp"} if any(TASKS[t].get("mcp") for t in tasks) else set())
                 | ({"_warm_agent"} if any(TASKS[t].get("tools") for t in tasks) else set()))
        with ThreadPoolExecutor(max_workers=3) as ex:
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
                        parent = (f"  parent {r['parent']['cost']:,}  report {r.get('report_chars', 0):,}"
                                  if "parent" in r else "")
                        if r.get("sub_first"):
                            parent += f"  sub_first write {r['sub_first']['cache_write']:,} read {r['sub_first']['cache_read']:,}"
                        status = "SKIP " + r["skip"] if r.get("skip") else "ok" if r["ok"] else "FAIL " + r["why"]
                        print(f"    {r['task']:<14} run {i + 1}: cost {u['cost']:>7,}  prod {u['cost_prod']:>7,}  "
                              f"turns {r['turns']}  in {u['input']:>6,}  "
                              f"cache_write {u['cache_write']:>6,}  cache_read {u['cache_read']:>8,}  "
                              f"out {u['output']:>6,}{parent}  {r['seconds']:>5.0f}s  {status}", flush=True)
    finally:
        if a.keep:
            print(f"kept run dirs in {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    out = {"model": a.model, "effort": EFFORT, "runs": a.runs, "claude_code": claude_version(), "config": a.config,
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
        if not r.get("skip"):
            by.setdefault(r["task"], []).append(r)
    return {t: {m: int(statistics.median(x["usage"][m] for x in rs)) for m in FIELDS}
            | ({"parent_cost": int(statistics.median(x["parent"]["cost"] for x in rs))}
               if all("parent" in x for x in rs) else {})
            | {"checks": f"{sum(x['ok'] for x in rs)}/{len(rs)}", "ok": all(x["ok"] or x.get("wrong") for x in rs)
               and sum(not x["ok"] for x in rs) <= int(len(rs) * TASKS.get(t, {}).get("wrong_ok", 0))}
            for t, rs in by.items()}


def values(runs, task, metric):
    return [r["usage"][metric] for r in runs if r["task"] == task]


def upper(vals, tol):
    """The task's higher level: runs split into levels at gaps over tol/2; the top of the highest level with at
    least two runs (a lone run is a one-off, like a prompt-cache miss), else the median."""
    levels = []
    for v in sorted(vals):
        if levels and v <= levels[-1][-1] * (1 + tol / 2):
            levels[-1].append(v)
        else:
            levels.append([v])
    return next((lv[-1] for lv in reversed(levels) if len(lv) > 1), statistics.median(vals))


def ratchet(c, top, tol):
    """Ceilings sit at the higher level * (1 + tol), so a release lowers one only when its costliest run is on that
    level (within the tol / 2 gap upper() splits levels at); a release whose runs all landed on a lower level leaves
    it alone. Checking against the higher level itself, not c, keeps a lowered ceiling from walking down a level."""
    return min(c, int(top * (1 + tol))) if top * (1 + tol) * (1 + tol / 2) >= c else c


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
    print(f"  {'task':<14} {'cost (median/ceiling)':>22}  {'in':>6} {'c_write':>7} {'c_read':>7} {'out':>6}  checks  result")
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
        cost = f"{k(m['cost'])}/{k(c['cost']) if c else '-'}" + ("!" if bad else "")
        raw = " ".join(f"{k(m[f]):>{w}}" for f, w in (("input", 6), ("cache_write", 7), ("cache_read", 7), ("output", 6)))
        print(f"  {t:<14} {cost:>22}  {raw}  {m['checks']:>6}  "
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
    if notes:
        print("  re-baseline: ceilings were measured with a different setup (" + "; ".join(notes) + "); run.py "
              "bootstrap on this setup, or the release script's --raise-ceiling TASK --reason ...")
    return 1 if fail else 0


def tol_for(ceil, task, metric):
    """Tolerance for one task and metric: {task: {metric: x}}, else a per-metric default {metric: x}."""
    t = ceil["tolerance"]
    per = t.get(task)
    return per.get(metric, FLOOR[metric]) if isinstance(per, dict) else t.get(metric, FLOOR[metric])


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
            new = ratchet(c[x], max(values(res["results"], t, x)), tol_for(ceil, t, x))
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
        tol = tol_for(ceil, a.task, x)
        new = int(upper(values(res["results"], a.task, x), tol) * (1 + tol))
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


def false_fail_rate(vals, tol, n=3, releases=20, trials=2000, seed=1):
    """Chance that an unchanged plugin fails the gate at least once in `releases` releases, drawing each run
    from `vals` (the measured runs), with ceilings bootstrapped at the higher level * (1 + tol) and lowered by the
    ratchet."""
    rng = random.Random(seed)
    boot = upper(vals, tol) * (1 + tol)
    fails = 0
    for _ in range(trials):
        c = boot
        for _ in range(releases):
            draw = [rng.choice(vals) for _ in range(n)]
            if statistics.median(draw) > c:
                fails += 1
                break
            c = ratchet(c, max(draw), tol)
    return fails / trials


# Lowest cost tolerance. Runs in one sitting agree more than runs hours apart, and some tasks flip between two modes
# a sitting may not show (log_triage takes one extra turn in some sittings, ~+15% cost; output length flips too).
FLOOR = {"cost": 0.2}


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
        # spread from every sitting given (--results and --spread-results), pooled as they are: scaling each sitting
        # by its own median would shift a two-level task's levels between sittings and blur them together
        pool = runs + [r for p in a.spread_results or [] for r in load(p)["results"]]
        tol = {t: {x: choose_tolerance(values(pool, t, x), n=RELEASE_RUNS, floor=FLOOR[x]) for x in METRICS}
               for t in sorted(by)}
    else:
        tol = {"cost": float(a.tolerance)}
    old = load(a.ceilings) if Path(a.ceilings).exists() else {}
    ceil = {"about": "Per-task cost ceilings (cost-equivalent tokens, see evals/tokens/run.py) for tokenmaxxing releases. "
                     "Medians must stay at or below these. Ceilings sit at each task's higher level * (1 + "
                     "tolerance) (some tasks take one extra turn in some runs); a release lowers one only when its "
                     "costliest run shows that level. Only the maintainer raises them (release script: "
                     "--raise-ceiling TASK --reason ...).",
            "tolerance": tol, "measured_with": setup_of(first), "tasks": {}, "history": old.get("history", [])}
    for t in sorted(med):
        ceil["tasks"][t] = {x: int(upper(values(runs, t, x), tol := tol_for(ceil, t, x)) * (1 + tol)) for x in METRICS}
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
    worst = {x: 0.0 for x in FIELDS}
    for t, rs in sorted(by.items()):
        line = [f"  {t:<14} n={len(rs):<2}"]
        for x in FIELDS:
            v = [r["usage"][x] for r in rs]
            med3 = [statistics.median(c) for c in itertools.combinations(v, 3)] if len(v) >= 3 else v
            ratio = max(med3) / max(1, min(med3)) - 1       # worst lucky-low vs unlucky-high median of 3
            worst[x] = max(worst[x], ratio)
            line.append(f"{x} {min(v):,}..{max(v):,} (med3 range +{ratio:.0%})")
        print("  ".join(line) + f"  checks {sum(r['ok'] for r in rs)}/{len(rs)}")
    print("  worst median-of-3 range: " + ", ".join(f"{x} +{worst[x]:.0%}" for x in FIELDS))
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
    m.add_argument("--config", default="",
                   help='TOML for the candidate\'s config, e.g. \'[handback]\\nmode = "off"\'')
    m.add_argument("--quiet", action="store_true")
    for name in ("gate", "lower", "raise", "bootstrap", "spread", "summary"):
        p = sub.add_parser(name)
        p.add_argument("--results", required=True, action="append" if name in ("bootstrap", "spread") else "store")
        p.add_argument("--ceilings", default=str(CEILINGS))
        p.add_argument("--sha", default="?")
        p.add_argument("--task")
        p.add_argument("--reason")
        p.add_argument("--tolerance", default="auto", help="auto (from the measured spread) or a fraction")
        p.add_argument("--spread-results", action="append", help="bootstrap: more sittings, for the spread only")
    a = ap.parse_args()
    if a.cmd == "measure":
        measure(a)
        return 0
    return {"gate": gate, "lower": lower, "raise": raise_, "bootstrap": bootstrap, "spread": spread,
            "summary": summary}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
