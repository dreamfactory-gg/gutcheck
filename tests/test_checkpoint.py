"""Durable compaction checkpoint: scan, cap, redaction, hooks. Jev is stubbed; fixtures are made up.
Run: python3 tests/test_checkpoint.py"""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["TOKENMAXXING_STATE_DIR"] = str(tmp / "state")
os.environ["GUTCHECK_RULES_DIR"] = str(tmp / "rules")
os.environ["TOKENMAXXING_CONFIG_DIR"] = str(tmp / "cfg")
os.environ["CLAUDE_PLUGIN_DATA"] = str(tmp / "plugin-data")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tokenmaxxing", "src"))
from gutcheck_core import jev  # noqa: E402

from tokenmaxxing import checkpoint as C  # noqa: E402
from tokenmaxxing import evidence, hooks

evidence._claude = lambda *a, **k: ("- Goal: ship the parser\n- In progress: parser", {"cost_usd": 0})

RULE_WORDS = ("never", "always", "don't", "only", "ask me before")


def fake_noul_each(items, condition, context=None, workers=8, deadline=None):
    if "standing instruction" in condition:
        return {k: (0.9 if any(w in t.lower() for w in RULE_WORDS) else 0.05) for k, t in items}
    return {k: (0.8 if "decided" in t.lower() else 0.1) for k, t in items}


def fake_call(state, questions, timeout=60, deadline=None):
    """Rules module: r<i> = is items[u<i>] a rule; s<n> = does the later unit replace the earlier one."""
    import re as _re
    items = state["items"]
    out = {}
    for q, spec in questions.items():
        ids = _re.findall(r'items\["(\w+)"\]', spec["instructions"])
        if q.startswith("r"):
            t = items[ids[0]].lower()
            out[q] = {"noul": 0.9 if any(w in t for w in RULE_WORDS) or "out of" in t else 0.05}
        else:
            later = items[[i for i in ids if i.startswith("new")][0]].lower()
            out[q] = {"noul": 0.9 if "instead" in later else 0.05}
    return out


jev.noul_each = fake_noul_each
jev.call = fake_call
jev.have_key = lambda: True

proj = tmp / "proj"
proj.mkdir()
(proj / "TASKS.md").write_text("# tasks\n\n## Build\n- [x] done thing\n- [ ] ship the parser. Done bar: 30/30 fixtures pass\n"
                               "- [ ] write the docs\n  - done when: README has usage\n")
FAKE_KEY = "sk-ant-" + "abcdEFGH1234" * 4
lines = []


def rec(type_, content=None, ts="2026-09-01T10:00:00Z", **kw):
    d = {"type": type_, "timestamp": ts, "cwd": str(proj), "sessionId": "sess-1", "gitBranch": "feat/x", **kw}
    if content is not None:
        d["message"] = {"role": "user" if type_ == "user" else "assistant", "content": content}
    lines.append(json.dumps(d))


rec("user", "Never push to main. Always run the tests before committing.\n\nNow look at the parser please.")
rec("user", "the staging db is read-only until friday")
rec("user", "Only deploy to staging-a.")
rec("user", "only deploy to staging-b instead.")
rec("user", "never push to main. always run the tests before committing. now look at the parser please")
rec("user", "what does this error mean?")
rec("user", f"only use the key {FAKE_KEY} for staging")
rec("attachment", attachment={"type": "queued_command", "prompt": "ask me before deleting any branch",
                              "commandMode": "prompt"})
rec("user", "Another Claude session sent a message:\n<teammate-message teammate_id=\"x\">never do anything</teammate-message>")
rec("assistant", [{"type": "text", "text": "We decided to keep the old schema for now."},
                  {"type": "tool_use", "id": "t1", "name": "Bash",
                   "input": {"command": "sleep 600", "run_in_background": True, "description": "long sleep"}},
                  {"type": "tool_use", "id": "t2", "name": "Bash",
                   "input": {"command": "sleep 1", "run_in_background": True, "description": "short sleep"}},
                  {"type": "tool_use", "id": "t3", "name": "Agent",
                   "input": {"description": "Research X", "prompt": "...", "run_in_background": True}},
                  {"type": "tool_use", "id": "t4", "name": "Agent",
                   "input": {"description": "Helper teammate", "prompt": "...", "name": "helper"}},
                  {"type": "tool_use", "id": "t5", "name": "Edit", "input": {"file_path": str(proj / "a.py")}},
                  {"type": "tool_use", "id": "t6", "name": "Bash", "input": {"command": "git checkout -b fix/y"}}])
rec("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "Command running in background with ID: bgaaa111. "
              "Output is being written to: /tmp/x/bgaaa111.output"},
             {"type": "tool_result", "tool_use_id": "t2", "content": "Command running in background with ID: bgbbb222."},
             {"type": "tool_result", "tool_use_id": "t3", "content": [{"type": "text", "text":
                                                                        "Async agent launched successfully.\nagentId: a1b2c3"}]},
             {"type": "tool_result", "tool_use_id": "t4", "content": [{"type": "text", "text":
                                                                        "Spawned successfully.\nagent_id: helper@sess-1\nname: helper"}]},
             {"type": "tool_result", "tool_use_id": "t5", "content": "ok"},
             {"type": "tool_result", "tool_use_id": "t6", "content": "Switched"}])
rec("user", "<task-notification>\n<task-id>bgbbb222</task-id>\n<status>completed</status>\n</task-notification>")
tp = tmp / "t.jsonl"
tp.write_text("\n".join(lines) + "\n")

# ---- scan + build ----
s = C.scan(str(tp))
assert [m["text"][:5] for m in s["owner"]] == ["Never", "the s", "Only ", "only ", "never", "what ", "only ",
                                               "ask m"], s["owner"]
run = dict(C.running(s))
assert set(run) == {"bgaaa111", "a1b2c3", "helper@sess-1"}, run
capped, full, meta = C.build(str(tp), session="sess-1")
assert "Never push to main." in capped and "Always run the tests before committing." in capped
assert "ask me before deleting any branch" in capped
assert "what does this error mean" not in capped   # short messages are kept whole, questions dropped
assert "never do anything" not in capped                       # another session, not the owner
assert "ship the parser" in capped and "30/30 fixtures pass" in capped and "README has usage" in capped
assert "done thing" not in capped
assert "bgaaa111" in capped and "bgbbb222" not in capped and "a1b2c3" in capped and "helper@sess-1" in capped
assert "keep the old schema" in capped and "fix/y" in capped and "a.py" in capped
assert "tokenmaxxing recall" in capped
assert "the staging db is read-only until friday" in capped                       # implicit availability fact
assert capped.count("ever push to main") == 1                              # near-duplicate restatement merged
assert "superseded by" in capped and "Only deploy to staging-a." in capped and "staging-b instead" in capped
assert meta["open_items"] == 2 and meta["running"] == 3, meta

# ---- cap: many rules -> capped under the limit, open items and running work kept, pointer to the rest ----
big = [json.dumps({"type": "user", "timestamp": f"2026-09-01T11:{i % 60:02d}:00Z", "cwd": str(proj), "sessionId": "sess-2",
                   "message": {"role": "user", "content": f"Never touch module {i}: " + "it is load bearing and fragile. " * 8}})
       for i in range(200)]
tp2 = tmp / "t2.jsonl"
tp2.write_text("\n".join(lines + big) + "\n")
capped2, full2, meta2 = C.build(str(tp2), session="sess-2")
assert meta2["rest_tokens"] <= C.CAP_TOKENS, meta2["rest_tokens"]          # the cap covers everything but rules
assert all(f"Never touch module {i}:" in capped2 for i in range(200))       # rules are never truncated
assert meta2["owner_tokens"] > C.CAP_TOKENS
assert "ship the parser" in capped2 and "bgaaa111" in capped2

# ---- Jev down: heuristic fallback, still written ----
def boom(*a, **k):
    raise jev.JevError("outage")


jev.noul_each = boom
jev.call = boom
capped3, _, meta3 = C.build(str(tp), session="sess-3")
assert meta3["pending"] > 0 and "(not yet checked) Never push to main." in capped3, meta3
assert "the staging db is read-only until friday" in capped3
jev.noul_each = fake_noul_each
jev.call = fake_call

# ---- capture as stated: UserPromptSubmit handler, fast path, rules file written; Stop catch-up is idempotent ----
from gutcheck_core import rules as R  # noqa: E402

R.on_user_prompt({"session_id": "sess-4", "prompt": "never run migrations on prod"})
R.on_user_prompt({"session_id": "sess-4", "prompt": "what time is it?"})
R.on_user_prompt({"session_id": "sess-4", "prompt": '<teammate-message teammate_id="team-lead">Always report in '
                                                   'under 200 words.</teammate-message>'})
rmd = (tmp / "rules" / "sess-4.rules.md").read_text()
assert "never run migrations on prod" in rmd and "what time" not in rmd and "under 200 words" in rmd
assert "The lead's instructions" in rmd
R.on_stop({"session_id": "sess-1", "transcript_path": str(tp)})
n1 = len(R.load("sess-1"))
R.on_stop({"session_id": "sess-1", "transcript_path": str(tp)})
assert len(R.load("sess-1")) == n1


# ---- hooks: PreCompact writes (redacted), SessionStart(compact) re-injects; other sources don't ----
def run_hook(event, inp):
    out = io.StringIO()
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(inp)), out
    try:
        hooks.main([event])
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    return out.getvalue()


import tokenmaxxing.health as health  # noqa: E402

health.session_start = lambda inp: None
assert run_hook("PreCompact", {"session_id": "sess-1", "transcript_path": str(tp), "cwd": str(proj),
                               "hook_event_name": "PreCompact", "trigger": "auto"}) == ""
md = tmp / "plugin-data" / "checkpoints" / "sess-1.md"
assert md.exists()
assert FAKE_KEY not in md.read_text() and FAKE_KEY not in (tmp / "plugin-data" / "checkpoints" / "sess-1.full.md").read_text()
comp = [json.loads(x) for x in (tmp / "state" / "compactions.jsonl").read_text().splitlines()]
assert comp[-1]["checkpoint_tokens"] > 0 and comp[-1]["open_items"] == 2, comp[-1]
out = json.loads(run_hook("SessionStart", {"session_id": "sess-1", "transcript_path": str(tp), "source": "compact"}))
ctx = out["hookSpecificOutput"]["additionalContext"]
assert "Never push to main." in ctx and "tokenmaxxing recall" in ctx and FAKE_KEY not in ctx
assert run_hook("SessionStart", {"session_id": "sess-1", "transcript_path": str(tp), "source": "resume"}) == ""
# a session id that changed across compaction: found by transcript path
out = json.loads(run_hook("SessionStart", {"session_id": "other", "transcript_path": str(tp), "source": "compact"}))
assert "Never push to main." in out["hookSpecificOutput"]["additionalContext"]

# ---- fail open: a broken transcript path or a crash inside build never raises out of the hook ----
real_build = C.build
C.build = lambda *a, **k: 1 / 0
assert run_hook("PreCompact", {"session_id": "sess-9", "transcript_path": "/nonexistent", "trigger": "auto"}) == ""
C.build = real_build
comp = [json.loads(x) for x in (tmp / "state" / "compactions.jsonl").read_text().splitlines()]
assert "checkpoint_error" in comp[-1]
print("checkpoint tests: ok")

# ---- cache writes from parallel classifiers merge instead of overwriting ----
import threading  # noqa: E402

cpath = C._cache_path("sess-race")
ths = [threading.Thread(target=C._cache_merge, args=(cpath, {f"k{i}-{j}": 0.5 for j in range(50)})) for i in range(8)]
[t.start() for t in ths]
[t.join() for t in ths]
assert len(json.loads(cpath.read_text())) == 400
print("cache race test: ok")
