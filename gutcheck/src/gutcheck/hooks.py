"""gutcheck's hooks: `gutcheck hook <Event>` reads the hook JSON on stdin, prints hook JSON (or nothing).

Every hook fails open: any exception means no output, and the error is logged.
"""
import json
import os
import sys
import time

from gutcheck_core import config, hookio
from gutcheck_core.hookio import PROMPT_DEADLINE, _safe, _T0, emit, log


# ---- PreToolUse: Bash ----------------------------------------------------------------------------
def pre_bash(inp):
    from gutcheck_core import secretscan, transcripts
    from . import bashcheck
    cmd = (inp.get("tool_input") or {}).get("command", "")
    cfg = config.load()
    if not cmd or key_guard(inp, cmd, cfg):
        return
    mode = cfg["bash_check"]["mode"]
    session = None
    # own-job detection needs the session's earlier commands; only read them when a kill is involved
    if any(w in cmd for w in ("kill", "pkill", "killall")) and inp.get("transcript_path"):
        session = transcripts.session_text(inp["transcript_path"])
    res = bashcheck.check(cmd, inp.get("cwd"), session=session)
    log("bashcheck", {"session": inp.get("session_id"), "tool_use_id": inp.get("tool_use_id"), "mode": mode,
                      "cwd": inp.get("cwd"), "cmd": secretscan.redact(cmd)[:4000],
                      "decision": res.get("decision"), "stage": res.get("stage"), "check": res.get("check"),
                      "reason": res.get("reason"), "probs": res.get("probs"), "error": res.get("error"),
                      "ms": res.get("ms")})
    out = {"hookEventName": "PreToolUse"}
    if mode == "on" and res.get("decision") == "ask":
        out.update(permissionDecision="ask", permissionDecisionReason=res["reason"])
    keep = delete_guard(inp, res.get("deletes"), cfg)
    if keep:
        out.update(permissionDecision="ask", permissionDecisionReason=keep)
    if len(out) > 1:
        emit({"hookSpecificOutput": out})


def pre_mcp(inp):
    """MCP PreToolUse: a literal key in the call's arguments is denied."""
    key_guard(inp, json.dumps(inp.get("tool_input") or {}, ensure_ascii=False), config.load())


def pre_edit(inp):
    """Write/Edit/MultiEdit: key guard on the new text (.env templates pass), don't-delete guard on a Write over an
    existing file."""
    from pathlib import Path

    from gutcheck_core import secretscan
    cfg = config.load()
    ti = inp.get("tool_input") or {}
    path = ti.get("file_path") or ""
    text = "\n".join([ti.get("content") or "", ti.get("new_string") or ""] +
                     [e.get("new_string") or "" for e in ti.get("edits") or []])
    if not secretscan.SAFE_ENV.search(path) and key_guard(inp, text, cfg):
        return
    p = Path(inp.get("cwd") or os.getcwd()) / Path(path).expanduser()
    keep = inp.get("tool_name") == "Write" and path and p.exists() and delete_guard(inp, [os.path.realpath(p)], cfg)
    if keep:
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                     "permissionDecisionReason": keep}})


def key_guard(inp, text, cfg):
    """A literal key in a tool's input is denied (placeholders and $VAR references pass). True when denied."""
    from gutcheck_core import secretscan
    mode = cfg["key_guard"]["mode"]
    kinds = mode != "off" and secretscan.classify_prompt(text, skip_placeholders=True)[0]
    if not kinds:
        return False
    log("key_guard", {"session": inp.get("session_id"), "tool": inp.get("tool_name"), "kinds": kinds, "mode": mode,
                      "action": "deny" if mode == "on" else "would-deny"})
    if mode == "on":
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                     "permissionDecisionReason":
                                         f"gutcheck: this {inp.get('tool_name')} input holds a literal secret "
                                         f"({', '.join(kinds)}), so it would be in the transcript for good. Pass it "
                                         "by name instead: `gutcheck withsecret <service> ENV_NAME -- command` puts "
                                         "the key in the command's environment as $ENV_NAME without showing it. "
                                         "For a test fixture, build the value at runtime (e.g. 'sk-' + 'ant-...') "
                                         "so no literal key is written."}})
    return mode == "on"


def delete_guard(inp, paths, cfg):
    """The ask reason when a call deletes or overwrites something the owner said not to delete, else None."""
    from gutcheck_core import rules
    mode = cfg["delete_guard"]["mode"]
    hit = mode != "off" and paths and rules.keep_hit(inp.get("session_id"), paths)
    if not hit:
        return None
    rule, path = hit
    log("delete_guard", {"session": inp.get("session_id"), "tool": inp.get("tool_name"), "path": path,
                         "rule": rule[:300], "mode": mode, "action": "ask" if mode == "on" else "would-ask"})
    if mode == "on":
        return (f'gutcheck: this deletes or overwrites {path}, and the owner said: "{rule[:300]}". '
                "Confirm with the owner before going ahead.")
    return None


def pre_read(inp):
    """Safety net behind the settings deny list: secret-shaped files (.env.*, keys, credentials) can't be read by
    Read or Grep, whatever their exact name; templates (.env.example, .sample, .template, .default) stay readable."""
    from gutcheck_core import secretscan
    ti = inp.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("path") or ""
    if path and secretscan.is_secret_file(os.path.basename(path.rstrip("/"))):
        log("secret_read_denied", {"session": inp.get("session_id"), "tool": inp.get("tool_name"),
                                   "file": os.path.basename(path)})
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                     "permissionDecisionReason":
                                         f"gutcheck: {os.path.basename(path)} holds secrets and isn't read into the "
                                         "conversation. Use key names only (`grep -o '^[A-Z_]*=' FILE`) or "
                                         "`gutcheck withsecret`."}})


# ---- PostToolUse: Bash ---------------------------------------------------------------------------
def post_bash(inp):
    """Record the approved Bash call and redact keys in its output. tokenmaxxing builds its trimmed views and saved
    files from redacted text itself (two plugins' PostToolUse hooks can't be ordered: the last to finish wins)."""
    from gutcheck_core import secretscan
    log("bash_ran", {"session": inp.get("session_id"), "tool_use_id": inp.get("tool_use_id")})
    resp = inp["tool_response"]
    mode = config.load()["key_guard"]["mode"]
    out = {} if mode == "off" else {k: r for k in ("stdout", "stderr") if isinstance(resp.get(k), str)
                                    and (r := secretscan.redact(resp[k], known_only=True)) != resp[k]}
    if out:
        log("key_guard", {"session": inp.get("session_id"), "tool": "Bash", "streams": sorted(out), "mode": mode,
                          "action": "redact" if mode == "on" else "would-redact"})
        if mode == "on":
            emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": {**resp, **out}}})


# ---- UserPromptSubmit: pasted keys ---------------------------------------------------------------
def prompt_submit(inp):
    from gutcheck_core import messages, secretscan, secrets
    cfg = config.load()
    warn_msg = delivered = None
    prompt = inp.get("prompt") or ""
    if cfg["keyblock"]["mode"] != "off":
        kinds, warn_kinds = secretscan.classify_prompt(prompt)
        if kinds and messages.owner_source(prompt) != "owner":
            # a subagent hand-back, peer or teammate message: only what the person typed is ever blocked
            log("keyblock", {"session": inp.get("session_id"), "kinds": kinds, "action": "delivered"})
            delivered = (f"gutcheck: a delivered message contained a key-shaped string ({', '.join(kinds)}). "
                         "Don't repeat it in tool calls or files.")
            kinds = warn_kinds = []
        if warn_kinds and not kinds:
            log("keyblock", {"session": inp.get("session_id"), "warn": warn_kinds, "action": "warn"})
            warn_msg = (f"gutcheck: this prompt contains a long random-looking string ({', '.join(warn_kinds)}). "
                        "If it's a key or token, rotate it and store it with " + secrets.store_hint("<name>") + ".")
        if kinds and _resent_within(prompt, 60):
            log("keyblock", {"session": inp.get("session_id"), "kinds": kinds, "action": "override-resent"})
            kinds = []  # the owner resent the same prompt within 60 s: their call
        if kinds:
            log("keyblock", {"session": inp.get("session_id"), "kinds": kinds, "mode": cfg["keyblock"]["mode"]})
            _remember_block(prompt)
            if cfg["keyblock"]["mode"] == "on":
                emit({"decision": "block",
                      "reason": ("gutcheck blocked this prompt: it contains what looks like a secret "
                                 f"({', '.join(kinds)}). It was not sent. Store the key instead with\n  "
                                 f"{secrets.store_hint('<name>')}\nand use it via "
                                 "`gutcheck withsecret <name> ENV_NAME -- command`. If you really mean to send it, "
                                 "send the exact same prompt again within 60 seconds.")})
                return
    # Standing-rules capture (the don't-delete guard reads it) may call Jev. It runs in a worker thread under a hard
    # deadline measured from process start, so a slow Jev never delays the prompt: whatever isn't classified by then
    # stays pending (backfilled at Stop) and the process exits at once.
    import threading
    from gutcheck_core import rules
    w = threading.Thread(target=_safe, args=("rules capture", rules.on_user_prompt, inp), daemon=True)
    w.start()
    w.join(max(0.0, PROMPT_DEADLINE - (time.monotonic() - _T0)))
    timed_out = w.is_alive()
    out = {}
    if warn_msg:
        out["systemMessage"] = warn_msg
    if delivered:
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": delivered}
    log("prompt_hook", {"session": inp.get("session_id"), "ms": int((time.monotonic() - _T0) * 1000),
                        "timed_out": timed_out})
    if out:
        emit(out)
    sys.stdout.flush()
    os._exit(0)  # don't wait for Jev worker threads still in flight


def _block_file():
    return config.STATE_DIR / "keyblock_last.json"


def _remember_block(prompt):
    """Store only a hash of the blocked prompt (never its text) and when it was blocked."""
    import hashlib
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        _block_file().write_text(json.dumps({"h": hashlib.sha256(prompt.encode()).hexdigest(), "t": time.time()}))
        os.chmod(_block_file(), 0o600)
    except OSError:
        pass


def _resent_within(prompt, secs):
    import hashlib
    try:
        d = json.loads(_block_file().read_text())
    except (OSError, ValueError):
        return False
    same = d.get("h") == hashlib.sha256(prompt.encode()).hexdigest() and time.time() - d.get("t", 0) <= secs
    if same:
        try:
            _block_file().unlink()  # one override per block
        except OSError:
            pass
    return same


def _block_file():
    return config.STATE_DIR / "keyblock_last.json"


def _remember_block(prompt):
    """Store only a hash of the blocked prompt (never its text) and when it was blocked."""
    import hashlib
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        _block_file().write_text(json.dumps({"h": hashlib.sha256(prompt.encode()).hexdigest(), "t": time.time()}))
        os.chmod(_block_file(), 0o600)
    except OSError:
        pass


def _resent_within(prompt, secs):
    import hashlib
    try:
        d = json.loads(_block_file().read_text())
    except (OSError, ValueError):
        return False
    same = d.get("h") == hashlib.sha256(prompt.encode()).hexdigest() and time.time() - d.get("t", 0) <= secs
    if same:
        try:
            _block_file().unlink()  # one override per block
        except OSError:
            pass
    return same


# ---- SessionStart, Stop --------------------------------------------------------------------------
def session_start(inp):
    from . import health
    text = health.session_start(inp)
    if text:
        emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})


def stop(inp):
    """Stop: standing-rules catch-up (queued prompts, the lead's messages to a teammate) for the don't-delete guard."""
    from gutcheck_core import rules
    _safe("rules catch-up", rules.on_stop, inp)


HANDLERS = {"PreToolUse:Bash": pre_bash, "PostToolUse:Bash": post_bash, "PreToolUse:mcp__": pre_mcp,
            "PreToolUse:Read": pre_read, "PreToolUse:Grep": pre_read, "UserPromptSubmit": prompt_submit,
            "SessionStart": session_start, "Stop": stop,
            "PrePush": lambda i: __import__("gutcheck.pushcheck", fromlist=["hook"]).hook(i),
            **dict.fromkeys(("PreToolUse:Edit", "PreToolUse:Write", "PreToolUse:MultiEdit"), pre_edit)}


def main(args):
    hookio.main(args, HANDLERS)
