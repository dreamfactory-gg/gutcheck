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


def results(meds, ok=True, runs=3):
    rs = []
    for t, (i, c, o) in meds.items():
        for n in range(runs):
            rs.append({"task": t, "run": n, "ok": ok, "why": "" if ok else "wrong answer", "models": ["m"],
                       "usage": {"input": i, "cache_read": c, "output": o}, "cost_usd": 0.01})
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


ceil = tmp / "ceilings.json"
# bootstrap: ceiling = median * (1 + tol)
rc, _ = call("bootstrap", "--results", results({"a": (1000, 10000, 200), "b": (500, 4000, 100)}),
             "--ceilings", str(ceil), "--sha", "s0", "--tolerance", "0.1,0.1,0.5")
c = json.loads(ceil.read_text())
assert rc == 0 and c["tasks"]["a"] == {"input": 1100, "cache_read": 11000, "output": 300}, c["tasks"]
assert len(c["history"]) == 6

# gate: at or under passes, over fails, a failed check fails
assert call("gate", "--results", results({"a": (1100, 11000, 300), "b": (500, 4000, 100)}), "--ceilings", str(ceil))[0] == 0
rc, out = call("gate", "--results", results({"a": (1101, 9000, 100), "b": (500, 4000, 100)}), "--ceilings", str(ceil))
assert rc == 1 and "over: input" in out, out
assert call("gate", "--results", results({"a": (900, 9000, 100), "b": (500, 4000, 100)}, ok=False),
            "--ceilings", str(ceil))[0] == 1

# lower: min(old, median * (1 + tol)); never up
rc, out = call("lower", "--results", results({"a": (800, 12000, 300), "b": (500, 4000, 100)}), "--ceilings", str(ceil),
               "--sha", "s1")
c = json.loads(ceil.read_text())
assert c["tasks"]["a"] == {"input": 880, "cache_read": 11000, "output": 300}, c["tasks"]["a"]
assert c["tasks"]["b"] == {"input": 550, "cache_read": 4400, "output": 150}
assert c["history"][-1]["why"] == "lowered by release" and c["history"][-1]["sha"] == "s1"
# a failed task never lowers
call("lower", "--results", results({"a": (10, 10, 10), "b": (10, 10, 10)}, ok=False), "--ceilings", str(ceil))
assert json.loads(ceil.read_text())["tasks"]["a"]["input"] == 880

# raise: only with a reason, only up, reason logged
assert call("raise", "--results", results({"a": (2000, 12000, 300)}), "--ceilings", str(ceil), "--task", "a")[0] != 0
rc, _ = call("raise", "--results", results({"a": (2000, 12000, 300)}), "--ceilings", str(ceil), "--task", "a",
             "--reason", "new Claude Code base prompt", "--sha", "s2")
c = json.loads(ceil.read_text())
assert rc == 0 and c["tasks"]["a"]["input"] == 2200 and c["tasks"]["a"]["cache_read"] == 13200
assert c["history"][-1]["why"].startswith("raised by owner: new Claude Code")

# auto tolerance: steady values get the smallest step (+1 for unseen tails), noisy ones a wider one
assert R.choose_tolerance([1000] * 12) == 0.1
assert R.choose_tolerance([1000, 1010, 990, 1400, 1000, 1005, 1395, 995, 1000, 1010, 1402, 1003]) >= 0.4
rc, _ = call("bootstrap", "--results", results({"a": (1000, 10000, 200)}, runs=12), "--ceilings", str(tmp / "c2.json"))
c2 = json.loads((tmp / "c2.json").read_text())
assert rc == 0 and c2["tolerance"]["a"]["input"] == 0.15 and c2["tasks"]["a"]["input"] == 1150, c2
assert c2["tolerance"]["a"]["output"] == 0.6 and c2["tasks"]["a"]["output"] == 320, c2

# the task list and its checks are well formed
assert set(R.TASKS) >= {"log_triage", "big_source", "one_line_edit", "mcp_log", "mcp_json"}
assert R.check_log("test_rounding_half_even\ntest_zone_lookup_unknown\ntest_token_refresh_race", None)[0]
assert not R.check_log("test_rounding_half_even\ntest_zone_lookup_unknown", None)[0]
assert R.check_source("1125", None)[0] and not R.check_source("800", None)[0]
assert R.check_mcp_json(R._stuck_id(), None)[0]

# GUTCHECK_DATA_DIR keeps plugin data out of the shared Claude Code plugin data dir
env = dict(os.environ, GUTCHECK_DATA_DIR=str(tmp / "data"), CLAUDE_PLUGIN_DATA=str(tmp / "cc-data"),
           GUTCHECK_STATE_DIR=str(tmp / "state"), PYTHONPATH=str(ROOT / "gutcheck" / "src"))
out = subprocess.run([sys.executable, "-c", "from gutcheck import skills, mcpout, checkpoint; "
                      "print(skills.data_dir(), mcpout.out_dir().parent, checkpoint.checkpoint_dir().parent)"],
                     env=env, capture_output=True, text=True, check=True).stdout.split()
assert out == [str(tmp / "data")] * 3, out
print("ok")
