"""Doc/memory loading switches on only when its logged background runs are fast and rarely fail. Made-up logs."""
import json, os, sys, tempfile, time
from pathlib import Path
d = Path(tempfile.mkdtemp())
os.environ["GUTCHECK_STATE_DIR"] = str(d)
os.environ["GUTCHECK_CONFIG_DIR"] = str(d / "cfg")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gutcheck", "src"))
from gutcheck import init  # noqa: E402


def logs(ms_list, err=0):
    t = time.strftime("%Y-%m-%dT%H:%M:%S")
    recs = [{"t": t, "background": True, "ms": m, "context": {}} for m in ms_list]
    for r in recs[:err]:
        r["context"] = {"error": "x"}
    (d / "router.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))


logs([2500] * 40)
u = {}
assert init._maybe_promote_context(u) is None and u["context"].get("mode", "log") == "log"      # slow: stays log
logs([400] * 40)
u = {}
assert init._maybe_promote_context(u) and u["context"]["mode"] == "on"                          # fast: on
logs([400] * 10)
u = {}
assert init._maybe_promote_context(u) is None                                                   # too few runs
logs([400] * 40, err=10)
u = {}
assert init._maybe_promote_context(u) is None                                                   # too many errors
u = {"context": {"mode": "log", "auto_promote": False}}
logs([400] * 40)
assert init._maybe_promote_context(u) is None                                                   # owner hold
print("ctx promote ok")
