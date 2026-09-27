"""Durable transitions: ticked items move to REPORT.md (open items untouched); the Now block; the restart / clear
pickup rule and sessions.json. Run: python3 tests/test_transitions.py"""
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["GUTCHECK_STATE_DIR"] = str(tmp / "state")
os.environ["GUTCHECK_CONFIG_DIR"] = str(tmp / "cfg")
os.environ.pop("CLAUDE_PLUGIN_DATA", None)
os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gutcheck", "src"))
from gutcheck import checkpoint, evidence, health, stopcheck
from gutcheck import transitions as T

write_now = T.write_now   # stubbed below for the transition cases
evidence._claude = lambda *a, **k: ("- Goal: new goal\n- Next step: y", {"cost_usd": 0})

tasks = tmp / "TASKS.md"
tasks.write_text("# Tasks\n\n## Now\n- Goal: x\n\n## Build\n- [x] parser\n  detail of parser\n- [ ] docs\n"
                 "- [X] parent\n  - [ ] open child\n- [x] lint\n")
assert T.move_ticked(tasks, today="2026-09-26") == 2
assert tasks.read_text() == ("# Tasks\n\n## Now\n- Goal: x\n\n## Build\n- [ ] docs\n- [X] parent\n  - [ ] open child\n")
report = (tmp / "REPORT.md").read_text()
assert report == "# Report\n\n## 2026-09-26 (moved from TASKS.md)\n- [x] parser\n  detail of parser\n- [x] lint\n", report
tasks.write_text("- [x] more\n")
assert T.move_ticked(tasks, today="2026-09-26") == 1 and tasks.read_text() == ""
assert (tmp / "REPORT.md").read_text().count("## 2026-09-26") == 1                # same day: same heading
assert T.move_ticked(tasks) == 0
assert T.now_block(tmp / "nope.md") is None
tasks.write_text("# T\n\n## Now\n- Goal: x\n- Next: y\n\nIntro paragraph.\n\n## Build\n")
assert T.now_block(tasks) == "## Now\n- Goal: x\n- Next: y\n\nIntro paragraph."      # up to the next heading

# fix 1: fences, loose lists, CRLF and no final newline, symlinks
tasks.write_text("```\n- [x] in a fence\n```\n- [x] loose\n\n  more of loose\n\n- [ ] open\n")
assert T.move_ticked(tasks) == 1
assert tasks.read_text() == "```\n- [x] in a fence\n```\n\n- [ ] open\n", tasks.read_text()
assert "- [x] loose\n\n  more of loose\n" in (tmp / "REPORT.md").read_text()
tasks.write_bytes(b"- [ ] b\r\n- [x] a\r\n  more\r\n- [ ] c")
assert T.move_ticked(tasks) == 1 and tasks.read_bytes() == b"- [ ] b\r\n- [ ] c"
(tmp / "real").mkdir()
(tmp / "real" / "TASKS.md").write_text("- [x] linked\n- [ ] kept\n")
(tmp / "link").mkdir()
(tmp / "link" / "TASKS.md").symlink_to(tmp / "real" / "TASKS.md")
assert T.move_ticked(tmp / "link" / "TASKS.md") == 1 and (tmp / "link" / "TASKS.md").is_symlink()
assert (tmp / "real" / "TASKS.md").read_text() == "- [ ] kept\n" and (tmp / "real" / "REPORT.md").exists()
assert not (tmp / "link" / "REPORT.md").exists()

# fix 2: the whole old block is replaced; what's injected is capped
tasks.write_text("# T\n\n## Now\n- Goal: old\n\nold para\n- old list\n## Build\n- [ ] x\n")
T.write_now({"owner": [], "agent_text": [], "bg": {}, "last_ts": None}, tasks)
new = tasks.read_text()
assert "old" not in new and "- Goal: new goal" in new and new.endswith("\n\n## Build\n- [ ] x\n"), new
tasks.write_text("## Now\n" + "".join(f"- line {i} " + "x" * 30 + "\n" for i in range(100)))
cut = T.now_block(tasks)
assert len(cut.splitlines()) <= T.NOW_MAX_LINES + 1 and len(cut) < T.NOW_MAX_CHARS + 200 and str(tasks) in cut

# fix 3 and 4: staleness is decided before the ticked items move; no model call on exit without a TASKS.md
calls = []
T.write_now = lambda s, tp, timeout=40: calls.append(timeout) or ("## Now\n- Goal: backup", {})
stopcheck.find_tasks = lambda *a: (tasks, None)
tasks.write_text("## Now\n- Goal: x\n\n## B\n- [x] done\n")
os.utime(tasks, (time.time() - 600,) * 2)
s = {"owner": [{"src": "owner", "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "text": "go"}],
     "cwd": str(tmp)}
assert T.prepare({"hook_event_name": "PreCompact"}, s) == "## Now\n- Goal: backup" and calls == [40]
assert "[x] done" not in tasks.read_text()
stopcheck.find_tasks = lambda *a: (None, None)
assert T.prepare({"hook_event_name": "SessionEnd", "reason": "prompt_input_exit"}, s) is None and len(calls) == 1
T.prepare({"hook_event_name": "SessionEnd", "reason": "clear"}, s)
assert calls == [40, 12]                                                          # /clear: short timeout
os.environ["CLAUDE_CODE_ENTRYPOINT"] = "bench"
assert not T._on()
os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"

cp = tmp / "cp.md"
cp.write_text("x")
t = 100_000.0
ended = {"session": "s1", "name": "parser work", "reason": "prompt_input_exit", "t": t - 3600, "checkpoint": str(cp)}
assert T.pickup("startup", ended, [], t).startswith("previous session parser work, s1")
assert T.pickup("startup", ended, ["s2"], t) is None                              # another session is live
assert T.pickup("startup", dict(ended, t=t - 7 * 3600), [], t) is None            # ended too long ago
assert T.pickup("resume", ended, [], t) is None
assert T.pickup("clear", ended, [], t) is None                                    # not a /clear
assert T.pickup("clear", dict(ended, reason="clear", t=t - 5), ["s2"], t) == "/clear"

# fix 5: SessionEnd records the session at once; a startup right after waits for its checkpoint, else a pointer
proj = str(tmp / "proj")
os.makedirs(proj)
T.checkpoint.scan = lambda p: {"owner": [{"src": "owner", "ts": None, "text": "hi"}], "titles": {}, "cwd": proj}
started = []
health._bg = lambda *a, **k: started.append(a)
T.on_end({"session_id": "s1", "reason": "prompt_input_exit", "hook_event_name": "SessionEnd", "cwd": proj})
assert started and T._sessions(lambda st: st["ended"][proj]["session"]) == "s1"
T.READY_WAIT = 0.5
out = T.on_start({"session_id": "s2", "source": "startup", "cwd": proj})
assert out.startswith("# WHERE WE ARE\nPrevious session s1 ended just now") and "s1.md" in out, out
cp = checkpoint.paths_for("s1")[0]
cp.parent.mkdir(parents=True, exist_ok=True)
T._sessions(lambda st: st["live"].clear())
threading.Timer(0.2, lambda: cp.write_text("# head\n## Now\n- Goal: parser")).start()
out = T.on_start({"session_id": "s3", "source": "startup", "cwd": proj})
assert out.startswith("# WHERE WE ARE (from previous session s1") and "Goal: parser" in out, out

# fix 6: a live entry whose Claude Code process is gone doesn't block pickup; a running one does
dead = subprocess.Popen(["true"])
dead.wait()
T._sessions(lambda st: st["live"].update(s3={"project": proj, "t": time.time(), "pid": dead.pid}))
assert T.on_start({"session_id": "s4", "source": "startup", "cwd": proj}).startswith("# WHERE WE ARE (from")
T._sessions(lambda st: st["live"].update(s4={"project": proj, "t": time.time(), "pid": os.getpid()}))
assert T.on_start({"session_id": "s5", "source": "startup", "cwd": proj}) is None

# fix 7: sessions.json of the wrong shape is empty; the compaction re-injection never depends on it
(T.config.STATE_DIR / "sessions.json").write_text("[]")
assert T.on_start({"session_id": "s6", "source": "startup", "cwd": proj}) is None
T.checkpoint.for_session_start = lambda inp: "CP"
T._sessions = lambda fn: 1 / 0
assert T.on_start({"session_id": "s6", "source": "compact", "cwd": proj}) == "CP"
# rewriting a stale Now block never touches task items under it, even after gutcheck inserted the block itself,
# and a fenced "# comment" doesn't end the block early
evidence._claude = lambda *a, **k: ("- Goal: NEW\n- Next step: NEW", {})
T._tail, T.checkpoint.running = (lambda s: ""), (lambda s: [])
nw = tmp / "now" / "TASKS.md"
nw.parent.mkdir()
for text, n, keep, gone in [
        ("# T\n\n## Now\n- Goal: OLD\n\n- [ ] fix the parser\n\n## Later\n- [ ] z\n", 1, ["fix the parser", "- [ ] z"], ["OLD"]),
        ("# Tasks\n- [ ] alpha\n- [ ] beta\n", 2, ["alpha", "beta"], []),
        ("# T\n## Now\n- Goal: OLD\n```sh\n# run this\nmake test\n```\n\n## Later\n- [ ] z\n", 1, ["## Later", "- [ ] z"],
         ["```", "make test"])]:
    nw.write_text(text)
    for _ in range(n):
        write_now({}, str(nw))
    out = nw.read_text()
    assert out.count("## Now") == 1 and all(k in out for k in keep) and not any(g in out for g in gone), out

print("transitions tests: ok")
