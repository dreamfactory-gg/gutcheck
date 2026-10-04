"""Hand-back reports: the brief asks for the verdict and everything the parent must act on first, and supporting
material under a `## Details` heading. The parent gets the part before that heading (at most 3,000 chars) plus the
path of the whole report, saved like a trimmed output. A report rides every later call of the parent, so its size
is paid on each of them. A report with no Details heading passes whole; nothing is ever refused.

Two delivery paths: a subagent with the SubagentHandback tool (auto mode) hands back through it (PreToolUse
SubagentHandback, rewritten via updatedInput). Without it (e.g. headless `claude -p`), the report is the Agent
tool's result (PostToolUse Agent, rewritten via updatedToolOutput).
"""
import re

from gutcheck_core import config
from gutcheck_core.hookio import emit, log

SHAPE = ("Start your final report with the verdict and everything your caller must act on (failures, ranked findings, "
         "answers); put supporting evidence, logs and long tables under a `## Details` heading in the same report, "
         "not in a file: your caller gets the part before it plus the path where the whole report is saved.")
WHOLE, HEAD = 2000, 3000
_DETAILS = re.compile(r"#{1,3}[ \t]*details\b", re.I)


def _mode():
    return config.load().get("handback", {}).get("mode", "on")


def brief(inp):
    """PreToolUse Agent: the tool input with the report shape appended to the brief, or None."""
    ti = inp.get("tool_input") or {}
    mode = _mode()
    if mode == "off" or not isinstance(ti.get("prompt"), str):
        return None
    log("handback", {"session": inp.get("session_id"), "decision": "brief", "mode": mode})
    return {**ti, "prompt": f"{ti['prompt']}\n\n{SHAPE}"} if mode == "on" else None


def _details_at(text):
    """Offset of the first Details heading outside code fences, or None."""
    pos, fence = 0, False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence and _DETAILS.match(line):
            return pos
        pos += len(line)
    return None


def cap(text):
    """(decision, text for the parent): whole, or head (the part before Details, cut to HEAD chars at a line
    boundary, plus the saved path)."""
    at = _details_at(text) if len(text) > WHOLE else None
    if at is None:
        return "whole", text
    from . import mcpout
    p = mcpout.save("handback", text, ".md")
    head = text[:at].rstrip()
    if len(head) > HEAD:
        head = head[:head.rfind("\n", 0, HEAD) + 1 or HEAD].rstrip() + f"\n\n(Cut at {HEAD:,} chars.)"
    return "head", (f"{head}\n\n(Full report with its Details, {len(text):,} chars, saved at {p}: grep it for the "
                    "part you need.)")


def _apply(inp, via, text, mode):
    decision, out = cap(text)
    log("handback", {"session": inp.get("session_id"), "agent": inp.get("agent_id") or
                     (inp.get("tool_response") or {}).get("agentId"), "via": via, "decision": decision,
                     "chars": len(text), "out_chars": len(out), "mode": mode})
    return out if mode == "on" and decision == "head" else None


def pre(inp):
    """PreToolUse SubagentHandback: the message becomes the part before Details plus the saved path."""
    mode = _mode()
    ti = inp.get("tool_input") or {}
    if mode != "off" and isinstance(ti.get("message"), str):
        out = _apply(inp, "SubagentHandback", ti["message"], mode)
        if out:
            emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {**ti, "message": out}}})


def post(inp):
    """PostToolUse Agent: a report that came back as the tool result (no SubagentHandback), the same way."""
    mode = _mode()
    resp = inp.get("tool_response")
    if mode == "off" or not isinstance(resp, dict) or resp.get("status") != "completed" or resp.get("handback"):
        return
    text = "\n".join(b.get("text", "") for b in resp.get("content") or [] if b.get("type") == "text")
    out = _apply(inp, "Agent", text, mode)
    if out:
        emit({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                     "updatedToolOutput": {**resp, "content": [{"type": "text", "text": out}]}}})
