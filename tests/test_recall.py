"""Recall (tree index + search), 'seen this error before?' and 'don't redo work', on made-up history
with a fake Jev. Run: python3 tests/test_recall.py"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# fixture days relative to today: recall searches a rolling window (30 days)
BASE = datetime.now(timezone.utc) - timedelta(days=5)


def day(n):
    return (BASE + timedelta(days=n)).strftime("%Y-%m-%d")


TMP = Path(tempfile.mkdtemp(prefix="gc-recall-")).resolve()
os.environ.update(TOKENMAXXING_STATE_DIR=str(TMP / "state"), TOKENMAXXING_CONFIG_DIR=str(TMP / "config"),
                  GUTCHECK_PROJECTS_DIR=str(TMP / "claude" / "projects"), GUTCHECK_CODEX_DIR=str(TMP / "codex"),
                  GUTCHECK_GIT_ROOT=str(TMP / "none"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tokenmaxxing", "src"))

from tokenmaxxing import index, recall  # noqa: E402
from gutcheck_core import jev  # noqa: E402

FAKE_KEY = "sk-ant-" + "abcDEF123456" * 4


def jl(path, recs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def user(text, ts, cwd="/work/proj"):
    return {"type": "user", "timestamp": ts, "cwd": cwd, "message": {"role": "user", "content": text}}


def asst(blocks, ts):
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": blocks}}


def result(tid, text, ts, err=False):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": err}]}}


proj = TMP / "claude" / "projects" / "-work-proj"
S1 = "11111111-aaaa-bbbb-cccc-000000000001"
jl(proj / f"{S1}.jsonl", [
    user("why does the widget cache return stale prices?", f"{day(1)}T10:00:00Z"),
    asst([{"type": "text", "text": "The widget cache keyed on sku only, so currency changes were ignored. "
                                    f"Fixed by adding currency to the cache key. token {FAKE_KEY}"}],
         f"{day(1)}T10:01:00Z"),
    asst([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q tests/test_cache.py"}}],
         f"{day(1)}T10:02:00Z"),
    result("t1", "Exit code 1\nE   ModuleNotFoundError: No module named 'frobnicate'", f"{day(1)}T10:02:05Z", True),
    asst([{"type": "text", "text": "The test venv lacks the optional frobnicate extra; installing it with "
                                    "`pip install .[frob]` fixes the import."}], f"{day(1)}T10:03:00Z"),
    asst([{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pip install .[frob]"}}],
         f"{day(1)}T10:03:10Z"),
    result("t2", "ok", f"{day(1)}T10:03:20Z"),
])
jl(proj / S1 / "subagents" / "agent-x1.jsonl", [
    user("Audit the billing module for rounding bugs in invoice totals and report file:line findings.",
         f"{day(2)}T09:00:00Z"),
    asst([{"type": "text", "text": "Found one: invoice.py:88 rounds each line before summing."}],
         f"{day(2)}T09:05:00Z"),
])
mem = proj / "memory"
mem.mkdir(parents=True)
(mem / "deploy-freeze.md").write_text("---\nname: deploy-freeze\n---\nWe decided to freeze deploys on Fridays "
                                      "after the outage; hotfixes need the owner's ok.\n")
(mem / "archive").mkdir()
(mem / "archive" / "old-queue.md").write_text("We moved the job queue from Redis to Postgres SKIP LOCKED because "
                                              "Redis lost jobs on failover.\n")
jl(TMP / "codex" / "sessions" / "2026" / "09" / "03" / "rollout-2026-09-03T01-00-00-x.jsonl", [
    {"type": "session_meta", "payload": {"cwd": "/work/proj", "timestamp": f"{day(3)}T01:00:00Z"}},
    {"type": "response_item", "timestamp": f"{day(3)}T01:00:01Z", "payload": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text":
            "Implement retry with jitter for the webhook sender in /work/proj/webhooks.py; add tests."}]}},
    {"type": "event_msg", "timestamp": f"{day(3)}T01:01:00Z", "payload": {"type": "item_completed", "item": {
        "type": "CommandExecution", "command": ["/bin/zsh", "-lc", "cargo build"], "exit_code": 101,
        "stdout": "", "stderr": "error: package collision in the lockfile: packages foo v0.1.0 and foo v0.1.0"}}},
    {"type": "response_item", "timestamp": f"{day(3)}T01:02:00Z", "payload": {
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text":
            "Cargo rejects symlinked path dependencies as distinct lockfile packages; pointing the manifest at "
            "the original paths fixed it."}]}},
    {"type": "event_msg", "timestamp": f"{day(3)}T01:09:00Z", "payload": {
        "type": "task_complete", "last_agent_message": "Retry with jitter added; 6 tests pass."}},
])

# ---- index -------------------------------------------------------------------------------------------
n = index.build()
assert n["claude"] == 2 and n["memory"] == 2 and n["codex"] == 1, n
assert index.build() == {"claude": 0, "memory": 0, "codex": 0, "git": 0}, "incremental build re-read sources"
db = index.connect()
alltext = " ".join(t for (t,) in db.execute("select text from chunks"))
assert FAKE_KEY not in alltext and "widget cache" in alltext, "chunks must be redacted and present"
assert db.execute("select count(*) from errs").fetchone()[0] == 2
kinds = sorted(k for (k,) in db.execute("select kind from launches"))
assert kinds == ["agent", "codex"], kinds
assert "archived" in " ".join(lab for (lab,) in db.execute("select label from nodes where level='session'"))

# ---- pure helpers ------------------------------------------------------------------------------------
assert recall.key_line("Exit code 1\nTraceback (most recent call last):\n  File x\nKeyError: 'data'") == "KeyError: 'data'"
assert recall.key_line("Exit code 1\n") is None
assert recall.key_line("Exit code 1\n- **Config** is read-only, STOP") is None
assert recall.key_line("<tool_use_error>Blocked: sleep</tool_use_error>") is None
brief = TMP / "brief.md"
brief.write_text("Do the thing to the webhook sender and more words here.")
assert recall.codex_brief(f"codex exec -m m --sandbox workspace-write - < {brief}") == brief.read_text()
assert recall.codex_brief("codex exec resume 1234") is None
assert recall.codex_brief("ls -la") is None
assert "digitalocean" not in recall.keywords("the Digital Ocean build")


# ---- search with a fake Jev -------------------------------------------------------------------------
def fake_noul_each(items, condition, context=None, deadline=None, **k):
    q = condition.lower()
    return {key: (0.9 if ("stale" in q and "currency" in text.lower()) or ("friday" in q and "friday" in text.lower())
                  else 0.05) for key, text in items}


jev.noul_each = fake_noul_each
res = recall.search("why were widget prices stale?")
assert res and "currency" in res[0]["excerpt"], res
assert res[0]["source"].startswith(str(proj / S1))
assert recall.search("what did we decide about deploys on friday?")[0]["source"].endswith("deploy-freeze.md:1")
assert recall.search("zzzz qqqq") == []


# ---- seen this error before? -------------------------------------------------------------------------
def fake_call(state, qs, deadline=None, **k):
    return {name: {"noul": 0.9} for name in qs}


jev.call = fake_call
out = recall.on_tool_failure({"tool_name": "Bash", "session_id": "22222222-aaaa-bbbb-cccc-000000000002",
                              "error": "Exit code 1\nModuleNotFoundError: No module named 'frobnicate'",
                              "tool_input": {"command": "pytest"}})
ctx = out["hookSpecificOutput"]["additionalContext"]
assert "frobnicate extra" in ctx and "tokenmaxxing recall --at" in ctx, ctx
# same session: silent
assert recall.on_tool_failure({"tool_name": "Bash", "session_id": S1, "tool_input": {"command": "pytest"},
                               "error": "Exit code 1\nModuleNotFoundError: No module named 'frobnicate'"}) is None
# no recognisable error line: silent
assert recall.on_tool_failure({"tool_name": "Bash", "session_id": "x" * 36, "error": "Exit code 1\n",
                               "tool_input": {"command": "grep x y"}}) is None
jev.call = lambda state, qs, **k: {name: {"noul": 0.3} for name in qs}
assert recall.on_tool_failure({"tool_name": "Bash", "session_id": "x" * 36, "tool_input": {"command": "pytest"},
                               "error": "Exit code 1\nModuleNotFoundError: No module named 'frobnicate'"}) is None
print(recall.show_at(ctx.rsplit("--at ", 1)[1])[:80].replace("\n", " "))

# ---- don't redo work -----------------------------------------------------------------------------------
jev.noul_each = lambda items, condition, context=None, **k: {
    key: (0.95 if "rounding" in text and "rounding" in (context or {}).get("NEW TASK", "") else 0.1)
    for key, text in items}
tp = TMP / "current.jsonl"
tp.write_text("")
agent_inp = {"tool_name": "Agent", "session_id": "33333333-aaaa-bbbb-cccc-000000000003", "transcript_path": str(tp),
             "tool_input": {"subagent_type": "general-purpose",
                            "prompt": "Audit the billing module for rounding bugs in invoice totals; report findings."}}
out = recall.on_launch(agent_inp)
assert out and "invoice.py:88" in out["hookSpecificOutput"]["additionalContext"], out
fork = dict(agent_inp, tool_input=dict(agent_inp["tool_input"], subagent_type="fork"))
assert recall.on_launch(fork) is None
# the session can still see that launch (it's in its transcript since the last compaction): silent
tp.write_text(json.dumps({"message": {"content": [{"type": "tool_use", "input": {
    "prompt": "Audit the billing module for rounding bugs in invoice totals and report file:line findings."}}]}}) + "\n")
assert recall.on_launch(agent_inp) is None
unrelated = dict(agent_inp, tool_input={"subagent_type": "general-purpose",
                                        "prompt": "Write release notes for the webhook retry change, two lines."})
assert recall.on_launch(unrelated) is None
print("test_recall: all passed")
