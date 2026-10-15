"""Stop check ladder with a stubbed classifier. Fixtures are made up. Run: python3 tests/test_stopcheck.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["TOKENMAXXING_STATE_DIR"] = str(tmp / "state")
os.environ["GUTCHECK_RULES_DIR"] = str(tmp / "rules")
os.environ["TOKENMAXXING_CONFIG_DIR"] = str(tmp / "cfg")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tokenmaxxing", "src"))
from gutcheck_core import config  # noqa: E402

from tokenmaxxing import stopcheck as S  # noqa: E402

(tmp / "cfg").mkdir()
(tmp / "cfg" / "config.toml").write_text('[stop]\nmode = "on"\njev_fallback = "on"\n')
config.load(refresh=True)
notes = []
S.notify = lambda line, cfg=None: notes.append(line)
LABEL = {"v": "announce"}


def fake_classify(message, open_items=(), crons=(), task=None, deadline=20.0):
    probs = {k: 0.01 for k in S.KINDS}
    probs[LABEL["v"]] = 0.92
    return S.decide(probs)


S.classify = fake_classify
proj = tmp / "proj"
proj.mkdir()
tr = proj / "t.jsonl"
tr.write_text("")
calls = []
S.classify = lambda *a, **k: calls.append(1) or fake_classify(*a, **k)


def append(rec):
    with tr.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def tool(name, **inp):
    append({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "x", "name": name, "input": inp}]}})


def stop(**extra):
    inp = {"session_id": "s1", "transcript_path": str(tr), "cwd": str(proj),
           "last_assistant_message": "Billing is half done. Next I'll migrate the rest.", **extra}
    return S.on_stop(inp)


os.environ["CLAUDE_LONG_RUN"] = "1"

# off unless CLAUDE_LONG_RUN=1
os.environ.pop("CLAUDE_LONG_RUN", None)
assert stop() is None
os.environ["CLAUDE_LONG_RUN"] = "1"

# running background work -> allow, not a stall
assert stop(background_tasks=[{"id": "b1", "type": "shell", "status": "running", "description": "tests"}]) is None
assert stop(background_tasks=[{"id": "b1", "type": "shell", "status": "completed"}])["decision"] == "block"

# an idle teammate isn't running work; an active one (from the transcript, when background_tasks lists none) is
assert stop(background_tasks=[{"id": "t1", "type": "teammate", "status": "idle"}])["decision"] == "block"
tm = proj / "team.jsonl"
tm.write_text(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "a1", "name": "Agent", "input": {"name": "builder", "prompt": "build it"}}]}}) + "\n" +
    json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "a1", "content": "Spawned successfully."}]}}) + "\n")
assert S.active_teammates(str(tm)) == ["builder"]
assert S.on_stop({"session_id": "s2", "transcript_path": str(tm), "cwd": str(proj),
                  "last_assistant_message": "Waiting on the builder."}) is None
with tm.open("a") as fh:
    fh.write(json.dumps({"type": "user", "message": {"content": '<teammate-message teammate_id="builder">'
                                                     '{"type":"idle_notification"}</teammate-message>'}}) + "\n")
assert S.active_teammates(str(tm)) == []

# an early stop gets the nudge for its kind
out = stop()
assert out["decision"] == "block" and S.NUDGE["announce"] in out["reason"]

# progress (an Edit) resets the count: a run that keeps progressing is nudged indefinitely
for _ in range(6):
    tool("Edit", file_path="a.py")
    assert stop()["decision"] == "block"
assert S.load_state("s1")["stalls"] == 0

# read-only Bash is not progress. The last nudge above plus two more with no progress = 3 nudges without
# progress: the cap. Then one BLOCKED nudge, then allow + STALLED.md + notify.
tool("Bash", command="ls -la")
r = [stop() for _ in range(4)]
assert [x and x["decision"] for x in r[:2]] == ["block"] * 2, r
assert "BLOCKED" in r[2]["reason"], r[2]
assert r[3] is None and (proj / "STALLED.md").exists() and notes, (r, notes)
stop()

# the owner typing resets the episode; our own nudge text doesn't
append({"type": "user", "message": {"content": S.MARK + " " + S.NUDGE["announce"]}})
stop()
assert S.load_state("s1")["stalls"] == 1
append({"type": "user", "message": {"content": "also rename the module"}})
stop()
assert S.load_state("s1")["stalls"] == 0

# legitimate stops are allowed and notify; done is allowed quietly
n = len(notes)
LABEL["v"] = "blocker"
assert stop() is None and len(notes) == n + 1
LABEL["v"] = "done"
assert stop() is None and len(notes) == n + 1

# log mode: never emits, logs would-block
(tmp / "cfg" / "config.toml").write_text('[stop]\nmode = "log"\n')
config.load(refresh=True)
LABEL["v"] = "offer"
assert stop() is None
last = json.loads((tmp / "state" / "stops.jsonl").read_text().splitlines()[-1])
assert last["action"] == "would-block" and last["mode"] == "log", last

print("ok")
