"""MCP layer: receipt mode views, waste-tracked levels, Roblox_Studio pass-through and the Roblox adapter's
rules, mirror switch, preload setting and old-profile removal. Made-up data only."""
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tokenmaxxing" / "src"))
_TMP = tempfile.mkdtemp()
os.environ["TOKENMAXXING_STATE_DIR"] = _TMP + "/state"
os.environ["TOKENMAXXING_CONFIG_DIR"] = _TMP + "/cfg"
os.environ["CLAUDE_PLUGIN_DATA"] = _TMP + "/data"
Path(_TMP, "cfg").mkdir()
Path(_TMP, "cfg", "config.toml").write_text('adapters = ["roblox_studio"]\n')

from gutcheck_core import config  # noqa: E402
from tokenmaxxing.adapters import roblox_studio as rs  # noqa: E402

from tokenmaxxing import hooks, mcpout  # noqa: E402

CFG = config.load(refresh=True)["mcp_trim"]


def test_action_receipt_ok_and_failure():
    ok = "Modified Thing (ModuleScript) with 1 edits applied\n" + "detail line\n" * 80
    v = mcpout.render("mcp__S__do_edit", ok, "action", "receipt", CFG)
    assert v.startswith("✓ ok") and "saved to" in v and len(v) < 300
    bad = "Ran 3 steps\n" + "fine\n" * 200 + "ServerScript.Foo:12: attempt to index nil with 'Parent'\nStack Begin\n"
    v = mcpout.render("mcp__S__do_edit", bad, "action", "receipt", CFG)
    assert v.startswith("✗") and "attempt to index nil" in v and "Stack Begin" in v


def test_read_is_never_a_bare_receipt():
    text = "\n".join(f"row {i}: value {i * 7}" for i in range(2000))
    v = mcpout.render("mcp__S__get_rows", text, "read", "receipt", CFG)  # clamped to outline
    assert "row 0" in v and "saved to" in v and not v.startswith("✓")


def test_headtail_keeps_middle_errors():
    text = "\n".join(f"line {i}" for i in range(3000)).replace("line 1500", "line 1500 ERROR: disk full")
    v = mcpout.render("mcp__S__dump", text, "read", "headtail", CFG)
    assert "disk full" in v


def test_log_view_errors_warnings_unexpected():
    boot = [f"[Config] Module{i}: {i} knobs, 0 missing" for i in range(40)]
    routine = {mcpout.line_key(l) for l in boot}
    text = "\n".join(boot + ["[S][WARN] price lookup slow", "Player joined: Alice", "Script 'X', Line 4",
                             "X:4: attempt to call a nil value"])
    v = mcpout.render("mcp__S__get_console_output", text, "log", "receipt", CFG, routine=routine)
    assert "attempt to call a nil value" in v and "price lookup slow" in v and "Player joined" in v
    assert "Module3:" not in v
    clean = "\n".join(boot)
    v = mcpout.render("mcp__S__get_console_output", clean, "log", "receipt", CFG, routine=routine)
    assert v.startswith("✓ no errors")


def test_kinds():
    assert mcpout.name_kind("mcp__S__get_console_output") == "log"
    assert mcpout.name_kind("mcp__S__multi_edit") == "action"
    assert mcpout.name_kind("mcp__S__script_read") == "read"


def _run_hook(event, inp):
    out = io.StringIO()
    old, sys.stdout = sys.stdout, out
    old_in, sys.stdin = sys.stdin, io.StringIO(json.dumps(inp))
    try:
        hooks.main([event])
    finally:
        sys.stdout, sys.stdin = old, old_in
    return json.loads(out.getvalue()) if out.getvalue() else None


def test_post_hook_receipt_for_action():
    text = "Created 40 parts\n" + "\n".join(f"part {i} at {i},0,0" for i in range(80))
    r = _run_hook("PostToolUse", {"tool_name": "mcp__Other__multi_edit", "session_id": "s",
                                  "tool_input": {"code": "make 40 parts"},
                                  "tool_response": [{"type": "text", "text": text}]})
    v = r["hookSpecificOutput"]["updatedMCPToolOutput"][0]["text"]
    assert v.startswith("✓ ok") and "saved to" in v


def test_studio_passes_through_untouched():
    """Roblox_Studio: no receipts, outlines or saved files, and nothing in front of the call."""
    saved = set(Path(_TMP, "data").rglob("*"))
    big = "\n".join(f"{i}: local x{i} = {i}" for i in range(1, 900))
    for tool, ti, text in [("script_read", {"target_file": "game.ServerScriptService.Big"}, big),
                           ("execute_luau", {"code": "for i=1,40 do Instance.new('Part', workspace) end"},
                            "Created 40 parts\n" + "x\n" * 400),
                           ("get_console_output", {}, "\n".join(f"[Boot] line {i}" for i in range(500))),
                           ("multi_edit", {"file_path": "x", "edits": []}, "Modified x with 1 edits applied\n" * 50)]:
        post = {"tool_name": f"mcp__Roblox_Studio__{tool}", "session_id": "s", "cwd": _TMP, "tool_input": ti,
                "tool_response": [{"type": "text", "text": text}]}
        assert _run_hook("PostToolUse", post) is None, tool
        pre = {"tool_name": f"mcp__Roblox_Studio__{tool}", "session_id": "s", "cwd": _TMP, "tool_input": ti,
               "transcript_path": "/nonexistent"}
        assert _run_hook("PreToolUse", pre) is None, tool  # no deny, no cache answer
    assert set(Path(_TMP, "data").rglob("*")) == saved  # nothing saved to a file
    assert not hasattr(rs, "pre_tool") and not hasattr(rs, "cached_state") and not hasattr(rs, "delegate")
    # the pass-through list is config: taking Studio off it brings the layer back
    cfg = Path(_TMP, "cfg", "config.toml")
    old = cfg.read_text()
    cfg.write_text(old + '[mcp_trim]\npass_through = []\n')
    config.load(refresh=True)
    try:
        post = {"tool_name": "mcp__Roblox_Studio__get_console_output", "session_id": "s", "cwd": _TMP,
                "tool_input": {}, "tool_response": [{"type": "text", "text": big}]}
        assert _run_hook("PostToolUse", post) is not None
    finally:
        cfg.write_text(old)
        config.load(refresh=True)


def test_mirror_off_by_default():
    called = []
    orig = rs._resync
    rs._resync = lambda ti: called.append(ti)
    try:
        rs.post_tool("multi_edit", {"tool_input": {"studio_id": "x"}}, "", {})
        assert called == []  # no resync after an edit unless [studio] mirror = true
        cfg = Path(_TMP, "cfg", "config.toml")
        old = cfg.read_text()
        cfg.write_text(old + '[studio]\nmirror = true\n')
        config.load(refresh=True)
        try:
            rs.post_tool("multi_edit", {"tool_input": {"studio_id": "x"}}, "", {})
            assert called == [{"studio_id": "x"}]
        finally:
            cfg.write_text(old)
            config.load(refresh=True)
    finally:
        rs._resync = orig
    try:
        rs.cli(["sync"])
        raise AssertionError("sync ran with the mirror off")
    except SystemExit as e:
        assert "mirror is off" in str(e.code)


def test_off_switch_env_and_project():
    text = "Created 40 parts\n" + "\n".join(f"part {i} at {i},0,0" for i in range(80))
    post = {"tool_name": "mcp__Other__multi_edit", "session_id": "s", "cwd": _TMP,
            "tool_input": {"code": "make a part"}, "tool_response": [{"type": "text", "text": text}]}
    assert _run_hook("PostToolUse", post)
    os.environ["TOKENMAXXING_MCP"] = "off"
    try:
        assert _run_hook("PostToolUse", post) is None
    finally:
        del os.environ["TOKENMAXXING_MCP"]
    proj = Path(_TMP, "proj")
    (proj / "sub").mkdir(parents=True, exist_ok=True)
    cfg = Path(_TMP, "cfg", "config.toml")
    old = cfg.read_text()
    cfg.write_text(old + f'[mcp]\noff_projects = ["{proj}"]\n')
    config.load(refresh=True)
    try:
        inside = str(proj / "sub")
        assert _run_hook("PostToolUse", {**post, "cwd": inside}) is None
        assert _run_hook("PostToolUse", post) is not None  # other directories keep the layer
    finally:
        cfg.write_text(old)
        config.load(refresh=True)
    logged = [json.loads(l) for l in Path(_TMP, "state", "mcp_trim.jsonl").read_text().splitlines()]
    assert any(d.get("skipped") == "off-switch" for d in logged)


def _rec(i, sim, live_level=None, reread=None):
    r = {"id": str(i), "t": 1e9 + i, "tool": "mcp__S__t", "key": "mcp__S__t", "kind": "read", "entered": sim["pass"][0],
         "used": 100, "sim": sim}
    if live_level:
        r.update(live_level=live_level, reread=reread)
    return r


def test_waste_tightens_and_loosens():
    from tokenmaxxing import mcpwaste
    # [view chars, hid a used line?, used chars shown, used chars hidden]
    wasteful = {"pass": [20000, 0, 100, 0], "headtail": [4000, 0, 100, 0], "outline": [1500, 0, 100, 0],
                "receipt": [1500, 0, 100, 0]}
    recs = [_rec(i, wasteful) for i in range(10)]
    assert mcpwaste.decide(recs, "pass", "read")[0] == "headtail"
    assert mcpwaste.decide(recs, "headtail", "read")[0] == "outline"
    assert mcpwaste.decide(recs, "outline", "read")[0] == "outline"  # read tools never get a bare receipt
    # live: saved files keep being re-read at outline -> loosen, if the looser view costs less overall
    reread = [_rec(i, wasteful, "outline", True) for i in range(10)]
    lv, why = mcpwaste.decide(reread, "outline", "read")
    assert lv == "headtail" and "re-read" in why
    # the tighter view would hide used lines too often -> stay
    hiding = {**wasteful, "outline": [1500, 1, 50, 900]}
    assert mcpwaste.decide([_rec(i, hiding) for i in range(10)], "headtail", "read")[0] == "headtail"


def test_old_profile_removed_only_if_ours():
    d = Path(tempfile.mkdtemp()) / "agents" / "studio.md"
    assert rs.remove_agent_profile(d) == "absent"
    d.parent.mkdir(parents=True)
    d.write_text("---\nname: studio\n---\nold\n\n" + rs.AGENT_MARK + "\n")
    assert rs.remove_agent_profile(d) == "removed" and not d.exists()
    d.write_text("---\nname: studio\n---\nthe owner's (or oof's) own")
    assert rs.remove_agent_profile(d).startswith("kept") and d.exists()
    assert not hasattr(rs, "install_agent") and not hasattr(rs, "agent_profile")


def test_project_rules_block_refresh():
    root = Path(tempfile.mkdtemp())
    a, b = root / "a", root / "b"
    a.mkdir()
    b.mkdir()
    (a / "CLAUDE.md").write_text("# A\nkeep me\n\n" + rs.BLOCK_START + "\nold rules\n" + rs.BLOCK_END + "\ntail\n")
    (b / "CLAUDE.md").write_text("# B\n")
    done = rs.install_rules([a, b], backup_root=root / "bak", refresh_only=True)
    assert done == [str(a / "CLAUDE.md")] and (b / "CLAUDE.md").read_text() == "# B\n"
    t = (a / "CLAUDE.md").read_text()
    assert t.startswith("# A\nkeep me\n") and t.endswith(rs.BLOCK_END + "\ntail\n") and "old rules" not in t
    for want in ("directly", "one turn", "Never poll", "SendMessage", "retire", "handoff note"):
        assert want in t, want
    assert "tokenmaxxing studio sync" not in t and (root / "bak" / "a" / "CLAUDE.md.before-studio-block").exists()
    assert rs.install_rules([a], backup_root=root / "bak", refresh_only=True) == []  # idempotent


def test_preload_on_off_keeps_owner_setting():
    from tokenmaxxing.adapters.roblox_studio import preload
    root = Path(tempfile.mkdtemp())
    cj, st = root / "claude.json", root / "state.json"
    srv = {"type": "stdio", "command": "/x/StudioMCP", "args": []}
    cj.write_text(json.dumps({"numStartups": 3, "projects": {
        "/p/a": {"mcpServers": {"Roblox_Studio": dict(srv)}, "other": 1},
        "/p/b": {"mcpServers": {"Roblox_Studio": {**srv, "alwaysLoad": True}}},
        "/p/c": {"mcpServers": {"Other": dict(srv)}}}}))
    assert preload.apply(True, cj, st, root / "bak") == ["/p/a"]
    d = json.loads(cj.read_text())
    assert d["projects"]["/p/a"]["mcpServers"]["Roblox_Studio"]["alwaysLoad"] is True
    assert "alwaysLoad" not in d["projects"]["/p/c"]["mcpServers"]["Other"] and d["numStartups"] == 3
    assert preload.status(cj, st) == {"entries": 2, "preloaded": 2, "by_gutcheck": 1}
    assert preload.apply(True, cj, st, root / "bak") == []  # idempotent
    assert preload.apply(False, cj, st, root / "bak") == ["/p/a"]
    d = json.loads(cj.read_text())
    assert "alwaysLoad" not in d["projects"]["/p/a"]["mcpServers"]["Roblox_Studio"]
    assert d["projects"]["/p/b"]["mcpServers"]["Roblox_Studio"]["alwaysLoad"] is True  # the owner's stays
    assert (root / "bak" / "claude.json.before-studio-preload").exists()


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
