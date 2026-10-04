"""Hook entry points: `gutcheck hook <Event>` reads the hook JSON on stdin, prints hook JSON (or nothing).

Every hook fails open: any exception means no output (today's behaviour), and the error is logged.
"""
import json
import os
import re
import sys
import time
import traceback

from . import config

LOG_DIR = config.STATE_DIR
_T0 = time.monotonic()
PROMPT_DEADLINE = float(os.environ.get("GUTCHECK_PROMPT_DEADLINE", "1.4"))  # seconds from hook start


def log(name, rec):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}
        with open(LOG_DIR / f"{name}.jsonl", "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def emit(obj):
    sys.stdout.write(json.dumps(obj))


# ---- PreToolUse: Bash ------------------------------------------------------------------------
def pre_bash(inp):
    from . import bashcheck, secretscan, transcripts
    cmd = (inp.get("tool_input") or {}).get("command", "")
    if not cmd:
        return
    cfg = config.load()
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
    nudge = loop_nudge(inp)
    out = {"hookEventName": "PreToolUse"}
    if mode == "on" and res.get("decision") == "ask":
        out.update(permissionDecision="ask", permissionDecisionReason=res["reason"])
    if nudge:
        out["additionalContext"] = nudge
    if re.search(r"\bcodex\b[^|;&\n]*\bexec\b", cmd):
        from . import recall
        d = _safe("dedup", recall.on_launch, inp)
        if d:
            out["additionalContext"] = "\n".join(filter(None, [out.get("additionalContext"),
                                                               d["hookSpecificOutput"]["additionalContext"]]))
    if len(out) > 1:
        emit({"hookSpecificOutput": out})


def pre_mcp(inp):
    """MCP PreToolUse: one-line hints only; calls themselves go through as they are (no routing, no cache).
    Studio calls get the Studio soldier hint (fresh agent per task above ~100k main context) instead of the
    general loop nudge; the MCP off switch silences it for stock comparisons."""
    from . import switches
    if str(inp.get("tool_name", "")).startswith("mcp__Roblox_Studio__"):
        nudge = None if switches.mcp_layer_off(inp) else studio_nudge(inp)
    else:
        nudge = loop_nudge(inp)
    if nudge:
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": nudge}})


FORK_LIMIT = 50_000


def context_tokens(transcript_path, max_bytes=2_000_000):
    """Context size from the latest assistant turn's usage in the transcript (input + cache read + cache write)."""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("isSidechain"):
            continue
        u = (d.get("message") or {}).get("usage") if isinstance(d.get("message"), dict) else None
        if u:
            return (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + \
                (u.get("cache_creation_input_tokens") or 0)
    return None


def pre_agent(inp):
    """Fork guard: a fork copies the whole context every turn; above ~50k, a fresh agent with a brief is cheaper."""
    ti = inp.get("tool_input") or {}
    if ti.get("subagent_type") != "fork":
        from . import recall
        out = _safe("dedup", recall.on_launch, inp)
        if out:
            emit(out)
        return
    n = context_tokens(inp.get("transcript_path") or "")
    limit = config.load().get("fork_guard", {}).get("max_context_tokens", FORK_LIMIT)
    log("fork_guard", {"session": inp.get("session_id"), "context_tokens": n, "denied": bool(n and n > limit)})
    if n and n > limit:
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                     "permissionDecisionReason":
                                         f"gutcheck fork guard: a fork would copy ~{n // 1000}k tokens of context "
                                         f"on every turn; brief a fresh agent instead (small self-contained brief, "
                                         f"absolute paths)."}})


def studio_nudge(inp):
    """Studio calls from the main session: above the Studio policy's main-context size, suggest a fresh soldier."""
    from . import adapters
    ad = adapters.for_server("Roblox_Studio")
    if ad is None or not hasattr(ad, "studio_hint") or config.load().get("soldier", {}).get("mode", "on") == "off":
        return None
    path = inp.get("transcript_path") or ""
    n_ctx = context_tokens(path)
    _, nudged = _calls_since_prompt(path, mark=ad.STUDIO_NUDGE_MARK)
    line = ad.studio_hint(inp, n_ctx, nudged)
    if line:
        log("studio_nudge", {"session": inp.get("session_id"), "context_tokens": n_ctx})
    return line


def loop_nudge(inp):
    """Tool-heavy loop in a very large main context: mention that a self-contained side task could go to a subagent
    (once per streak). Worded so it never reads as a reason to stop or hand off the main work."""
    if inp.get("agent_id"):  # already inside a subagent
        return None
    cfg = config.load().get("loop_nudge", {})
    if cfg.get("mode", "on") == "off":
        return None
    path = inp.get("transcript_path") or ""
    n_ctx = context_tokens(path)
    if not n_ctx or n_ctx < cfg.get("min_context_tokens", 400_000):
        return None
    calls, nudged = _calls_since_prompt(path)
    if calls < cfg.get("min_calls", 8) or nudged:
        return None
    log("loop_nudge", {"session": inp.get("session_id"), "context_tokens": n_ctx, "calls": calls})
    return (f"gutcheck: long tool loop at ~{n_ctx // 1000}k tokens. If the rest is a self-contained side task, a "
            f"subagent can take it (skill `gutcheck:soldiers`); otherwise carry on.")


def _user_text(text):
    return isinstance(text, str) and not text.lstrip().startswith("<")


def _calls_since_prompt(path, max_bytes=1_500_000, mark="gutcheck: long tool loop"):
    """(tool calls in the main thread since the owner's last prompt, whether a nudge was already given)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return 0, False
    calls, nudged = 0, False
    for line in reversed(lines):
        if '"isSidechain":true' in line:
            continue
        if mark in line:
            nudged = True
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message") or {}
        c = m.get("content") if isinstance(m, dict) else None
        if d.get("type") == "user":
            if _user_text(c):
                break
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "text" and
                                           _user_text(b.get("text", "")) for b in c):
                break
        elif d.get("type") == "assistant" and isinstance(c, list):
            calls += sum(1 for b in c if isinstance(b, dict) and b.get("type") == "tool_use")
    return calls, nudged


def pre_read(inp):
    """Safety net behind the settings deny list: secret-shaped files (.env.*, keys, credentials) can't be read by
    Read or Grep, whatever their exact name; templates (.env.example, .sample, .template, .default) stay readable.
    Read gate: the first whole-file Read in a session of a text file whose first 2,000 lines (what Read returns)
    exceed read_gate.min_chars is denied with its size and how to read a range; the identical retry goes through."""
    from . import secretscan
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
        return
    if inp.get("tool_name") != "Read" or not path or any(k in ti for k in ("offset", "limit", "pages")):
        return
    import hashlib
    from pathlib import Path
    cfg = config.load().get("read_gate", {})
    mode = cfg.get("mode", "on")
    limit = cfg.get("min_chars", 30000)
    if mode == "off":
        return
    p = (Path(inp.get("cwd") or os.getcwd()) / Path(path).expanduser()).resolve()
    if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
                            ".svg", ".ico", ".avif", ".heic", ".pdf", ".ipynb"):
        return
    try:
        if not p.is_file() or os.path.getsize(p) <= limit:
            return
        with p.open(encoding="utf-8", newline="") as fh:
            text = fh.read()
    except (OSError, UnicodeError):
        return
    lines = text.splitlines(keepends=True)
    if len(lines) < 10 or sum(map(len, lines[:2000])) <= limit:
        return
    session = inp.get("session_id")
    marker = config.STATE_DIR / "read_gate" / hashlib.sha256(json.dumps([session, str(p)]).encode()).hexdigest()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        retry = False
    except FileExistsError:
        retry = True
    except OSError:
        return
    log("read_gate", {"session": session, "agent": inp.get("agent_id"), "path": str(p), "chars": len(text),
                      "mode": mode, "decision": "retry" if retry else "denied"})
    if mode == "on" and not retry:
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                     "permissionDecisionReason":
                                         f"gutcheck: {p} has {len(lines):,} lines and {len(text):,} chars. "
                                         "Find the needed lines with grep -n, then Read that range with offset/limit. "
                                         "Edit and Write need a Read of the part they change. "
                                         "If the whole file really is needed, repeat the same Read and it goes through."}})


def post_failure(inp):
    """A failed Bash call: 'seen this error before?' from past sessions (silent unless confident)."""
    from . import recall
    log("tool_failures", {"session": inp.get("session_id"), "tool": inp.get("tool_name")})
    out = _safe("seen-error", recall.on_tool_failure, inp)
    if out:
        emit(out)


def post_bash(inp):
    """Record the approved Bash call and compact long output."""
    log("bash_ran", {"session": inp.get("session_id"), "tool_use_id": inp.get("tool_use_id")})
    post_tool_trim(inp)


def post_tool_trim(inp):
    import shlex
    from pathlib import Path
    from . import mcpout
    cfg = config.load().get("tool_trim", {})
    mode = cfg.get("mode", "on")
    if mode == "off":
        return
    name = inp["tool_name"]
    resp = inp["tool_response"]
    if resp.get("interrupted") or resp.get("isImage"):
        return
    cmd = shlex.shlex((inp.get("tool_input") or {}).get("command", ""), posix=True, punctuation_chars=True)
    cmd.whitespace_split = True
    cwd = Path(inp.get("cwd") or os.getcwd())
    saved_dir = mcpout.out_dir().resolve()
    if any((cwd / Path(arg).expanduser()).resolve().is_relative_to(saved_dir) for arg in cmd):
        return
    ti = inp.get("tool_input") or {}
    try:
        purpose = _purpose(inp, include_user=not inp.get("agent_id"))
        brief = _purpose(inp, include_user=True) if inp.get("agent_id") else ""
    except Exception:
        purpose, brief = "", ""
    out = {}
    for key in ("stdout", "stderr"):
        text = resp[key]
        view = mcpout.render(name, text, "read", mcpout.DEFAULT_LEVEL["read"], cfg,
                             lambda ext: mcpout.save(name, text, ext), purpose=purpose, command=ti.get("command", ""),
                             brief=brief, description=ti.get("description", ""), compact=True)
        if view is not None:
            out[key] = view
            log("tool_trim", {"session": inp.get("session_id"), "tool_use_id": inp.get("tool_use_id"),
                              "agent": inp.get("agent_id"), "mode": mode, "tool": name,
                              "chars": len(text), "out_chars": len(view)})
    if out and mode == "on":
        emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": {**resp, **out}}})


# ---- PostToolUse: any MCP output (core + adapters) ------------------------------------------------
def post_mcp(inp):
    """MCP output goes to a file; context gets what matters (receipt mode). The server's adapter first, then the
    core: the call's kind (action/log/read) and the tool's current level from the waste-tracked policy
    (mcp_policy.json) pick the view. Images and pass-through servers (Roblox_Studio) are left untouched."""
    from . import adapters, mcpout, switches
    name = inp.get("tool_name", "")
    parts = name.split("__")
    if len(parts) < 3:
        return
    if switches.mcp_layer_off(inp):
        log("mcp_trim", {"session": inp.get("session_id"), "tool": name, "skipped": "off-switch"})
        return
    server, tool = parts[1], "__".join(parts[2:])
    cfg = config.load().get("mcp_trim", {})
    if mcpout.passes_through(server, cfg):
        ad = adapters.for_server(server)
        if ad is not None:
            ad.post_tool(tool, inp, "", cfg)  # side effects only (e.g. the opt-in Studio mirror resync)
        return
    mode = cfg.get("mode", "on")
    resp = inp.get("tool_response")
    if mcpout.has_images(resp):
        return  # screenshots and other images pass through untouched
    if isinstance(resp, dict) and (resp.get("isError") or resp.get("is_error")):
        return  # failed calls pass through whole
    text = mcpout.text_of(resp)
    ti = inp.get("tool_input") or {}
    out, how, kind, level = None, None, None, None
    ad = adapters.for_server(server)
    if ad is not None:
        out = ad.post_tool(tool, inp, text, cfg)
        how = "adapter" if out else None
    if out is None and text:
        kind, per_call = mcpout.kind_for(name, ti, text, ad)
        key = mcpout.policy_key(name, kind, per_call)
        level = mcpout.level_for(key, kind)
        routine = mcpout.routine_for(name) if kind == "log" else None
        judge = _log_judge(inp, name) if kind == "log" and cfg.get("log_judge", "on") == "on" else None
        out = mcpout.render(name, text, kind, level, cfg, lambda ext: mcpout.save(name, text, ext), routine, judge)
        if kind == "log":
            try:
                mcpout.routine_update(name, text)
            except OSError:
                pass
        how = "core" if out else None
    if out is None:
        return
    log("mcp_trim", {"session": inp.get("session_id"), "tool_use_id": inp.get("tool_use_id"),
                     "agent": inp.get("agent_id"), "mode": mode, "tool": name,
                     "via": how, "kind": kind, "level": level, "chars": len(text), "out_chars": len(out)})
    if mode == "on":
        emit({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                     "updatedMCPToolOutput": [{"type": "text", "text": out}]}})


def _purpose(inp, max_chars=400, include_user=False):
    """The agent's words before this call, optionally with the latest plain-string user prompt."""
    path = inp.get("transcript_path") or ""
    if inp.get("agent_id") and path:
        from pathlib import Path
        p = Path(path).with_suffix("") / "subagents" / f"agent-{inp['agent_id']}.jsonl"
        path = str(p) if p.exists() else path
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if not include_user:
                fh.seek(max(0, size - 300_000))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    purpose = ""
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message") or {}
        content = m.get("content")
        if include_user and d.get("type") == "user" and _user_text(content):
            return " ".join([purpose, content])
        if not purpose and d.get("type") == "assistant" and isinstance(content, list):
            txt = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            if txt.strip():
                purpose = txt.strip()[-max_chars:]
                if not include_user:
                    return purpose
    return purpose


def _log_judge(inp, name):
    """Jev noul over a log's unseen lines: given the call's stated purpose, does the line show a failure or
    something unexpected? Fails open (all unseen lines shown) on any Jev problem."""
    def judge(lines):
        from . import jev
        if not jev.have_key():
            return lines
        purpose = _purpose(inp) or "(not stated)"
        items = [(n, ln[:300]) for n, ln in lines[:150]]
        probs = jev.noul_each(items, "Given the purpose of the call that produced this log line, does the line "
                                     "show a failure, an error, or something unexpected the agent should see?",
                              context={"tool": name, "purpose": purpose}, deadline=5.0)
        keep = [(n, ln) for n, ln in lines[:150] if probs.get(n, probs.get(str(n), 1.0)) >= 0.4]
        return keep + lines[150:]
    return judge


# ---- UserPromptSubmit: pasted keys -------------------------------------------------------------
def prompt_submit(inp):
    from . import secretscan, secrets
    cfg = config.load()
    warn_msg = None
    prompt = inp.get("prompt") or ""
    if cfg["keyblock"]["mode"] != "off":
        kinds, warn_kinds = secretscan.classify_prompt(prompt)
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
    # Everything below may call Jev. It runs in a worker thread under a hard deadline measured from process start,
    # so a slow Jev never delays the prompt: whatever isn't ready by then is dropped (rules get backfilled at
    # Stop/PreCompact) and the process exits at once.
    import threading
    res = {}

    def work():
        from . import rules as _rules
        from . import router
        t = threading.Thread(target=_safe, args=("rules capture", _rules.on_user_prompt, inp), daemon=True)
        t.start()
        res["extra"] = _safe("router", router.for_prompt, inp)
        t.join(max(0.0, PROMPT_DEADLINE - (time.monotonic() - _T0)))

    w = threading.Thread(target=work, daemon=True)
    w.start()
    w.join(max(0.0, PROMPT_DEADLINE - (time.monotonic() - _T0)))
    timed_out = w.is_alive()
    out = {}
    if warn_msg:
        out["systemMessage"] = warn_msg
    extra = None if timed_out else res.get("extra")
    if extra:
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": extra}
    log("prompt_hook", {"session": inp.get("session_id"), "ms": int((time.monotonic() - _T0) * 1000),
                        "timed_out": timed_out})
    if out:
        emit(out)
    sys.stdout.flush()
    os._exit(0)  # don't wait for Jev worker threads still in flight


def _block_file():
    return LOG_DIR / "keyblock_last.json"


def _remember_block(prompt):
    """Store only a hash of the blocked prompt (never its text) and when it was blocked."""
    import hashlib
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
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


def _safe(name, fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        log("hook_errors", {"event": name, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]})


# ---- SessionStart ------------------------------------------------------------------------------
def session_start(inp):
    from . import health
    parts = [reinject(inp)]
    try:
        parts.append(health.session_start(inp))
    except Exception as e:
        log("hook_errors", {"event": "SessionStart", "error": f"{type(e).__name__}: {e}"})
    text = "\n\n".join(p for p in parts if p)
    if text:
        emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})


def reinject(inp):
    """After a compaction, a /clear or a restart: the checkpoint written at the transition (transitions.py), so the
    Now block, open items, standing rules and running work survive."""
    from . import checkpoint, transitions
    try:
        text = transitions.on_start(inp)
    except Exception as e:
        log("hook_errors", {"event": f"SessionStart:{inp.get('source')}", "error": f"{type(e).__name__}: {e}",
                            "trace": traceback.format_exc()[-1500:]})
        return None
    if text or inp.get("source") == "compact":
        log("checkpoint_inject", {"session": inp.get("session_id"), "source": inp.get("source"), "found": bool(text),
                                  "tokens": checkpoint.est_tokens(text) if text else 0})
    return text


def stop(inp):
    from . import rules
    _safe("rules catch-up", rules.on_stop, inp)
    from . import stopcheck
    out = stopcheck.on_stop(inp)
    if out:
        emit(out)


def compact_log(inp):
    """PreCompact: write the durable checkpoint (re-injected at SessionStart after the compaction), and record the
    compaction (trigger, context size just before, checkpoint size) for `gutcheck report`. Fails open."""
    rec = {"session": inp.get("session_id"), "event": inp.get("hook_event_name"), "trigger": inp.get("trigger"),
           "context_tokens": context_tokens(inp.get("transcript_path") or "")}
    try:
        from . import checkpoint
        meta = checkpoint.write(inp)
        rec.update(checkpoint_tokens=meta.get("tokens"), checkpoint_full_tokens=meta.get("full_tokens"),
                   rules=meta.get("rules"), open_items=meta.get("open_items"), running=meta.get("running"),
                   rule_how=meta.get("rule_how"), checkpoint_ms=meta.get("ms"),
                   rules_tokens=meta.get("rules_tokens"), lead_tokens=meta.get("lead_tokens"),
                   rest_tokens=meta.get("rest_tokens"))
    except Exception as e:
        rec["checkpoint_error"] = f"{type(e).__name__}: {e}"
        log("hook_errors", {"event": "PreCompact", "error": rec["checkpoint_error"],
                            "trace": traceback.format_exc()[-1500:]})
    log("compactions", rec)


def subagent_start(inp):
    """A replacement agent starts from its retired predecessor's handoff note (soldier.py)."""
    from . import soldier
    ctx = _safe("soldier note in", soldier.start_context, inp)
    if ctx:
        log("soldier", {"session": inp.get("session_id"), "agent": inp.get("agent_id"),
                        "agent_type": inp.get("agent_type"), "note_in": len(ctx)})
        emit({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": ctx}})


def subagent_stop(inp):
    """A retiring agent's handoff note is saved; then the evidence check on its report."""
    from . import soldier
    p = _safe("soldier note out", soldier.capture, inp)
    if p:
        log("soldier", {"session": inp.get("session_id"), "agent": inp.get("agent_id"),
                        "agent_type": inp.get("agent_type"), "note_out": str(p)})
        return  # a handoff note isn't a report to fact-check
    __import__("gutcheck.evidence", fromlist=["hook"]).hook(inp)


def pre_send(inp):
    """SendMessage to an agent past its reuse thresholds: one line suggesting retirement (never blocks)."""
    from . import soldier
    dec = _safe("soldier check", soldier.check_send, inp) or {}
    if dec.get("_rec"):
        log("soldier", {"session": inp.get("session_id"), **dec["_rec"]})
    if dec.get("additionalContext"):
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": dec["additionalContext"]}})


HANDLERS = {"PreCompact": compact_log, "PreToolUse:Bash": pre_bash, "PreToolUse:Agent": pre_agent, "PostToolUse:Bash": post_bash, "PostToolUseFailure:Bash": post_failure,
            "PreToolUse:Read": pre_read, "PreToolUse:Grep": pre_read,
 "UserPromptSubmit": prompt_submit,
            "SessionStart": session_start, "Stop": stop,
            "SessionEnd": lambda i: __import__("gutcheck.transitions", fromlist=["on_end"]).on_end(i),
            "SubagentStop": subagent_stop, "SubagentStart": subagent_start, "PreToolUse:SendMessage": pre_send,
            "PrePush": lambda i: __import__("gutcheck.pushcheck", fromlist=["hook"]).hook(i),
            **dict.fromkeys(("PostToolUse:Edit", "PostToolUse:Write", "PostToolUse:MultiEdit"),
                            lambda i: __import__("gutcheck.lint", fromlist=["hook"]).hook(i)),
            **dict.fromkeys(("PreToolUse:Edit", "PreToolUse:Write", "PreToolUse:MultiEdit"),
                            lambda i: __import__("gutcheck.lint", fromlist=["pre"]).pre(i))}


def main(args):
    event = args[0] if args else ""
    try:
        inp = json.load(sys.stdin)
    except Exception:
        return
    key = event
    if event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        key = f"{event}:{inp.get('tool_name')}"
    h = HANDLERS.get(key)
    if h is None and str(inp.get("tool_name", "")).startswith("mcp__"):
        h = pre_mcp if event == "PreToolUse" else post_mcp if event == "PostToolUse" else None
    if not h:
        return
    try:
        h(inp)
    except Exception as e:
        log("hook_errors", {"event": key, "error": f"{type(e).__name__}: {e}",
                            "trace": traceback.format_exc()[-1500:]})
