"""Token ratchet arithmetic (evals/tokens/run.py) on made-up results; no Claude calls, plus the data-dir override.
Run: python3 tests/test_token_ratchet.py"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "evals" / "tokens" / "run.py"
tmp = Path(tempfile.mkdtemp())
sys.path.insert(0, str(RUN.parent))
import run as R  # noqa: E402


def results(costs, ok=True, runs=3, wrong=()):
    """costs: {task: cost, or a list with one cost per run}; wrong: (task, run) pairs that answered wrong."""
    rs = [{"task": t, "run": n, "ok": ok and (t, n) not in wrong, "wrong": (t, n) in wrong,
           "why": "" if ok else "hook guard", "models": ["m"],
           "usage": {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "cost": c}, "cost_usd": 0.01}
          for t, cs in costs.items() for n, c in enumerate(cs if isinstance(cs, list) else [cs] * runs)]
    d = {"model": "sonnet", "effort": "low", "runs": runs, "claude_code": "x", "seconds": 1, "cost_usd": 0.1,
         "results": rs, "medians": R.medians(rs)}
    p = tmp / f"r{len(list(tmp.iterdir()))}.json"
    p.write_text(json.dumps(d))
    return str(p)


def call(*argv):
    sys.argv = ["run.py", *argv]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            rc = R.main()
        except SystemExit as e:
            rc = e.code
    return rc, buf.getvalue()


# cost-equivalent tokens from the raw counts
assert int(sum(n * R.WEIGHTS[f] for f, n in {"input": 100, "cache_write": 400, "cache_read": 10000, "output": 50}.items())) == 1850

ceil = tmp / "ceilings.json"
# bootstrap: ceiling = median * (1 + tol)
rc, _ = call("bootstrap", "--results", results({"a": 10000, "b": 4000}), "--ceilings", str(ceil), "--sha", "s0",
             "--tolerance", "0.1")
c = json.loads(ceil.read_text())
assert rc == 0 and c["tasks"] == {"a": {"cost": 11000}, "b": {"cost": 4400}}, c["tasks"]
assert len(c["history"]) == 2

# gate: at or under passes, over fails, a failed check fails, a new Claude Code says re-baseline
assert call("gate", "--results", results({"a": 11000, "b": 4000}), "--ceilings", str(ceil))[0] == 0
rc, out = call("gate", "--results", results({"a": 11001, "b": 4000}), "--ceilings", str(ceil))
assert rc == 1 and "over: cost" in out, out
assert call("gate", "--results", results({"a": 9000, "b": 4000}, ok=False), "--ceilings", str(ceil))[0] == 1
c["measured_with"]["claude_code"] = "old"
ceil.write_text(json.dumps(c))
rc, out = call("gate", "--results", results({"a": 9000, "b": 4000}), "--ceilings", str(ceil))
assert rc == 0 and "re-baseline" in out and "Claude Code old -> x" in out, out

# lower follows the higher level: a (ceiling 11000 = level 10000 * 1.1) lowers only when the costliest run is on
# that level (>= 10000 / 1.05); runs that all landed on a lower level, a one-off above it, or going up change nothing
rc, out = call("lower", "--results", results({"a": [8000, 8000, 9000], "b": 5000}), "--ceilings", str(ceil))
assert json.loads(ceil.read_text())["tasks"] == {"a": {"cost": 11000}, "b": {"cost": 4400}}, out
rc, out = call("lower", "--results", results({"a": [8000, 9600, 8000], "b": 4000}), "--ceilings", str(ceil), "--sha", "s1")
c = json.loads(ceil.read_text())
assert c["tasks"] == {"a": {"cost": 10560}, "b": {"cost": 4400}}, c["tasks"]
assert c["history"][-1]["why"] == "lowered by release" and c["history"][-1]["sha"] == "s1"
# the lowered ceiling (level 9600) doesn't walk down to the lower level on the next all-low release
call("lower", "--results", results({"a": [8000, 8000, 8000]}), "--ceilings", str(ceil))
assert json.loads(ceil.read_text())["tasks"]["a"]["cost"] == 10560
# a failed task never lowers
call("lower", "--results", results({"a": 9500, "b": 10}, ok=False), "--ceilings", str(ceil))
assert json.loads(ceil.read_text())["tasks"]["a"]["cost"] == 10560

# wrong answers: big_source tolerates 1 in 3, other tasks none; a harness failure is never tolerated
med = R.medians(json.loads(Path(results({"big_source": 1, "log_triage": 1}, wrong={("big_source", 1)})).read_text())["results"])
assert med["big_source"]["ok"] and med["big_source"]["checks"] == "2/3", med
w2 = {("big_source", 0), ("big_source", 1), ("log_triage", 2)}
med = R.medians(json.loads(Path(results({"big_source": 1, "log_triage": 1}, wrong=w2)).read_text())["results"])
assert not med["big_source"]["ok"] and not med["log_triage"]["ok"], med
assert not R.medians(json.loads(Path(results({"big_source": 1}, ok=False)).read_text())["results"])["big_source"]["ok"]

# raise: only with a reason, only up, from the higher level (the lone 20000 is a one-off), reason logged
assert call("raise", "--results", results({"a": 12000}), "--ceilings", str(ceil), "--task", "a")[0] != 0
rc, _ = call("raise", "--results", results({"a": [12000, 12000, 20000]}), "--ceilings", str(ceil), "--task", "a",
             "--reason", "new Claude Code base prompt", "--sha", "s2")
c = json.loads(ceil.read_text())
assert rc == 0 and c["tasks"]["a"]["cost"] == 13200
assert c["history"][-1]["why"].startswith("raised by owner: new Claude Code")

# the higher level; a two-level task stays under a 25% ceiling through 20 releases of the ratchet
assert R.upper([7900, 8000, 10000, 10100, 14000], 0.25) == 10100 and R.upper([5, 7, 9], 0.25) == 7
assert R.false_fail_rate([7900, 8000, 8100] * 3 + [10000, 10100] * 3, 0.25) == 0
assert R.choose_tolerance([1000] * 12, floor=0.2) == 0.25
rc, _ = call("bootstrap", "--results", results({"a": 10000}, runs=12), "--ceilings", str(ceil))
c2 = json.loads(ceil.read_text())
assert rc == 0 and c2["tolerance"]["a"]["cost"] == 0.25 and c2["tasks"]["a"]["cost"] == 12500, c2
assert c2["history"][:len(c["history"])] == c["history"], "a re-bootstrap keeps the history"

# the task list and its checks are well formed
assert set(R.TASKS) >= {"log_triage", "big_source", "one_line_edit", "mcp_log", "mcp_json"}
assert R.check_log("test_rounding_half_even\ntest_zone_lookup_unknown\ntest_token_refresh_race", None)[0]
assert not R.check_log("test_rounding_half_even\ntest_zone_lookup_unknown", None)[0]
assert R.check_source("1125", None)[0] and not R.check_source("800", None)[0]
assert R.check_mcp_json(R._stuck_id(), None)[0]

# TOKENMAXXING_DATA_DIR keeps plugin data out of the shared Claude Code plugin data dir
env = dict(os.environ, TOKENMAXXING_DATA_DIR=str(tmp / "data"), CLAUDE_PLUGIN_DATA=str(tmp / "cc-data"),
           TOKENMAXXING_STATE_DIR=str(tmp / "state"), PYTHONPATH=str(ROOT / "tokenmaxxing" / "src"))
out = subprocess.run([sys.executable, "-c", "from tokenmaxxing import skills, mcpout, checkpoint; "
                      "print(skills.data_dir(), mcpout.out_dir().parent, checkpoint.checkpoint_dir().parent)"],
                     env=env, capture_output=True, text=True, check=True).stdout.split()
assert out == [str(tmp / "data")] * 3, out
print("ok")
