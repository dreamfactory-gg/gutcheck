"""Hand-back reports: the brief asks for a `## Summary` up front, and the parent gets only that Summary plus the path
of the full report, saved like a trimmed output. A report rides every later call of the parent, so its size is paid
on each of them.

Two delivery paths: a subagent with the SubagentHandback tool (auto mode) hands back through it (PreToolUse
SubagentHandback, rewritten via updatedInput; an unstructured long report is denied once there). Without it (e.g.
headless `claude -p`), the report is the Agent tool's result (PostToolUse Agent, rewritten via updatedToolOutput).
"""
import re

from gutcheck_core import config
from gutcheck_core.hookio import emit, log

LINE = ("\n\nStart your final report with `## Summary` (at most 1,500 chars, with the verdict or answer and what's "
        "left) and put everything else under `## Details`: your caller sees only the Summary plus a path to the "
        "full report.")
WHOLE, SUMMARY, UNSTRUCTURED = 2000, 1500, 3000
_SUMMARY = re.compile(r"^##[ \t]*Summary[ \t]*:?[ \t]*\n(.*?)(?=^##[ \t]|\Z)", re.M | re.S | re.I)


def _mode():
    return config.load().get("handback", {}).get("mode", "on")


def brief(inp):
    """PreToolUse Agent: the tool input with the report-shape line appended to the brief, or None."""
    ti = inp.get("tool_input") or {}
    mode = _mode()
    if mode == "off" or not isinstance(ti.get("prompt"), str):
        return None
    log("handback", {"session": inp.get("session_id"), "decision": "brief", "mode": mode})
    return {**ti, "prompt": ti["prompt"] + LINE} if mode == "on" else None


def cap(text):
    """(decision, text for the parent): whole, summary (Summary + saved path) or unstructured (long, no Summary)."""
    m = _SUMMARY.search(text)
    if len(text) <= WHOLE or not m and len(text) <= UNSTRUCTURED:
        return "whole", text
    if not m:
        return "unstructured", text
    from . import mcpout
    p = mcpout.save("handback", text, ".md")
    s = m.group(1).strip()
    s = s if len(s) <= SUMMARY else s[:SUMMARY - 1] + "…"
    return "summary", f"{s}\n\n(Full report, {len(text):,} chars, saved at {p}: grep it for the part you need.)"


def _record(inp, via, text, decision, out, mode):
    log("handback", {"session": inp.get("session_id"), "agent": inp.get("agent_id") or
                     (inp.get("tool_response") or {}).get("agentId"), "via": via, "decision": decision,
                     "chars": len(text), "out_chars": len(out), "mode": mode})


def pre(inp):
    """PreToolUse SubagentHandback: the message becomes Summary + path; an unstructured long one is denied once
    (the next attempt passes whole, so it never loops)."""
    from .hooks import once
    mode = _mode()
    ti = inp.get("tool_input") or {}
    text = ti.get("message")
    if mode == "off" or not isinstance(text, str):
        return
    decision, out = cap(text)
    if decision == "unstructured" and mode == "on":
        decision = "denied" if once("handback", inp.get("session_id"), inp.get("agent_id")) else "whole"
    _record(inp, "SubagentHandback", text, decision, out, mode)
    if mode != "on" or decision == "whole":
        return
    if decision == "denied":
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                     "permissionDecisionReason":
                                         f"tokenmaxxing: this report is {len(text):,} chars with no `## Summary`. "
                                         "Start it with `## Summary` (at most 1,500 chars, with the verdict or "
                                         "answer and what's left), put the rest under `## Details`, and hand it "
                                         "back again; your caller sees the Summary plus a path to the full "
                                         "report."}})
        return
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {**ti, "message": out}}})


def post(inp):
    """PostToolUse Agent: a report that came back as the tool result (no SubagentHandback) becomes Summary + path.
    No deny is possible here, so an unstructured report passes whole."""
    mode = _mode()
    resp = inp.get("tool_response")
    if mode == "off" or not isinstance(resp, dict) or resp.get("status") != "completed" or resp.get("handback"):
        return
    text = "\n".join(b.get("text", "") for b in resp.get("content") or [] if b.get("type") == "text")
    decision, out = cap(text)
    _record(inp, "Agent", text, decision, out, mode)
    if mode == "on" and decision == "summary":
        emit({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                     "updatedToolOutput": {**resp, "content": [{"type": "text", "text": out}]}}})
