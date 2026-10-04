"""Persistent soldiers (any subagent): the handoff note saved on retirement, handed once to the replacement, and
the one-line retirement suggestion when a resumed agent is past its thresholds. Made-up transcripts only."""
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tokenmaxxing" / "src"))
_TMP = tempfile.mkdtemp()
os.environ["TOKENMAXXING_STATE_DIR"] = _TMP + "/state"
os.environ["TOKENMAXXING_CONFIG_DIR"] = _TMP + "/cfg"
os.environ["CLAUDE_PLUGIN_DATA"] = _TMP + "/data"
Path(_TMP, "cfg").mkdir()
Path(_TMP, "cfg", "config.toml").write_text('[soldier.agents.bigctx]\nmax_context_tokens = 500000\n')

from gutcheck_core import config  # noqa: E402

from tokenmaxxing import hooks, soldier  # noqa: E402

config.load(refresh=True)
PROJ = Path(_TMP, "proj")
PROJ.mkdir()


def _run_hook(event, inp):
    out = io.StringIO()
    old, sys.stdout = sys.stdout, out
    old_in, sys.stdin = sys.stdin, io.StringIO(json.dumps(inp))
    try:
        hooks.main([event])
    finally:
        sys.stdout, sys.stdin = old, old_in
    return json.loads(out.getvalue()) if out.getvalue() else None


NOTE = ("HANDOFF NOTE\n- Damage is applied in ServerScriptService.Combat.Damage:41-58\n- open: crit multiplier "
        "ignores armor")


def test_note_saved_then_handed_over_once():
    stop = {"session_id": "s1", "cwd": str(PROJ), "agent_id": "a1", "agent_type": "studio",
            "agent_transcript_path": "/nonexistent", "last_assistant_message": NOTE, "stop_hook_active": False}
    assert _run_hook("SubagentStop", stop) is None
    p = soldier.note_path(PROJ, "studio")
    assert p.exists() and "Combat.Damage:41-58" in p.read_text() and str(PROJ) not in str(p.parent.parent)
    start = {"session_id": "s1", "cwd": str(PROJ), "agent_id": "a2", "agent_type": "studio",
             "hook_event_name": "SubagentStart"}
    other = _run_hook("SubagentStart", {**start, "agent_type": "general-purpose"})
    assert other is None  # another agent type doesn't get it
    assert _run_hook("SubagentStart", {**start, "cwd": _TMP}) is None  # another project doesn't either
    r = _run_hook("SubagentStart", start)
    ctx = r["hookSpecificOutput"]["additionalContext"]
    assert r["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
    assert "Combat.Damage:41-58" in ctx and "previous studio agent" in ctx
    assert _run_hook("SubagentStart", start) is None  # handed over once
    assert soldier.start_context({**start, "agent_id": "a3"}) is None


def test_ordinary_reply_is_not_a_note():
    assert soldier.capture({"cwd": str(PROJ), "agent_type": "explorer",
                            "last_assistant_message": "Found it. The handoff note idea: HANDOFF NOTE later"}) is None
    assert not soldier.note_path(PROJ, "explorer").exists()
    assert soldier.capture({"cwd": str(PROJ), "agent_type": "explorer",
                            "last_assistant_message": "```\n" + NOTE + "\n```"}) is not None  # fenced is fine


def _session(name, atype, ctx_tokens, idle_min):
    root = Path(tempfile.mkdtemp())
    main = root / "sess.jsonl"
    main.write_text("")
    sub = root / "sess" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-abc123def.meta.json").write_text(json.dumps({"agentType": atype, "name": name}))
    t = sub / "agent-abc123def.jsonl"
    t.write_text(json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 10, "cache_read_input_tokens": ctx_tokens - 10, "cache_creation_input_tokens": 0},
        "content": [{"type": "text", "text": "done"}]}}) + "\n")
    old = time.time() - idle_min * 60
    os.utime(t, (old, old))
    return str(main)


def _send(tp, to, msg="also check the shop script"):
    return {"session_id": "s", "cwd": str(PROJ), "tool_name": "SendMessage", "transcript_path": tp,
            "tool_input": {"to": to, "message": msg}}


def test_retirement_suggested_past_thresholds_never_blocks():
    warm = _session("studio", "studio", 20_000, 1)
    assert _run_hook("PreToolUse", _send(warm, "studio")) is None
    big = _session("studio", "studio", 300_000, 1)
    r = _run_hook("PreToolUse", _send(big, "studio"))["hookSpecificOutput"]
    assert "permissionDecision" not in r and "~300k" in r["additionalContext"] and "retire" in r["additionalContext"]
    assert soldier.NOTE_MARK in r["additionalContext"]
    cold = _session("studio", "studio", 20_000, 75)
    r = _run_hook("PreToolUse", _send(cold, "abc123"))["hookSpecificOutput"]  # addressed by id prefix
    assert "idle 75 min" in r["additionalContext"]
    assert _run_hook("PreToolUse", _send(big, "studio", soldier.RETIRE_MSG)) is None  # the retirement itself
    assert _run_hook("PreToolUse", _send(big, "someone-else")) is None
    assert _run_hook("PreToolUse", {**_send(big, "studio"), "agent_id": "x"}) is None  # main session only
    # per-agent thresholds (by name or type) from config
    roomy = _session("bigctx", "general-purpose", 150_000, 1)
    assert _run_hook("PreToolUse", _send(roomy, "bigctx")) is None
    assert soldier.thresholds("bigctx", "general-purpose")["max_context_tokens"] == 500_000
    assert soldier.thresholds(None, "studio")["max_idle_minutes"] == soldier.default_idle_minutes()


def test_cli_prints_latest_note():
    soldier.capture({"cwd": str(PROJ), "agent_type": "studio", "last_assistant_message": NOTE})
    out = io.StringIO()
    old, sys.stdout = sys.stdout, out
    try:
        soldier.cli(["--dir", str(PROJ)])
        soldier.cli(["studio", "--path", "--dir", str(PROJ)])
    finally:
        sys.stdout = old
    lines = out.getvalue().splitlines()
    assert "pending" in lines[0] and "Combat.Damage:41-58" in out.getvalue()
    assert lines[-1] == str(soldier.note_path(PROJ, "studio"))


if __name__ == "__main__":
    fails = 0
    for n, f in list(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f()
                print("ok  ", n)
            except Exception as e:
                fails += 1
                import traceback
                print("FAIL", n, "".join(traceback.format_exception(e))[-800:])
    sys.exit(1 if fails else 0)
