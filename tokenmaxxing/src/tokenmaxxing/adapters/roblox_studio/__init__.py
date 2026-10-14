"""Roblox Studio adapter. Studio calls go direct and come back untouched: the core MCP output layer passes
Roblox_Studio through (config `mcp_trim.pass_through`), with no receipts, outlines or saved files; oof, when
installed, shapes Studio output at the source. What the adapter still does:
  - the Studio rules: rules.md;
  - removing the `studio` agent profile earlier versions installed (oof owns the Studio agent now);
  - preloading the Studio tools in Roblox projects instead of deferring them behind ToolSearch (preload.py);
  - the experimental local script mirror (studio.py): `tokenmaxxing studio sync` and the resync after an edit, both off
    unless `[studio] mirror = true`."""
from pathlib import Path

SERVER = "Roblox_Studio"
# oof's eval: Studio work moves to a fresh foreground agent per task once the main context is above ~100k (below
# that, call Studio directly), and a studio agent is never resumed: a SendMessage resume runs in the background and
# costs the main session ~2 extra turns per hand-off, which a warm, same-area reuse arm still lost on.
SOLDIER_POLICIES = {r"studio": {"reuse": False, "fresh_above_main_tokens": 100_000}}
STUDIO_NUDGE_MARK = "tokenmaxxing: main context is"


def studio_hint(inp, main_tokens, already_nudged):
    """One line for a direct Studio call from a large main session: hand this task to a fresh studio agent."""
    th = SOLDIER_POLICIES[r"studio"]["fresh_above_main_tokens"]
    if inp.get("agent_id") or not main_tokens or main_tokens < th or already_nudged:
        return None
    return (f"{STUDIO_NUDGE_MARK} ~{main_tokens // 1000}k, so Studio output here rides along on every later call. "
            f"For the rest of this Studio task, start a fresh studio agent with a short brief (goal, Studio id, "
            f"scripts, how to verify), in the foreground, and take back only its conclusion. Start a new one for the "
            f"next Studio task instead of resuming it (a resume runs in the background and costs extra main turns).")


def _cfg():
    from gutcheck_core import config
    return config.load().get("studio", {})


def mirror_on():
    return bool(_cfg().get("mirror", False))


def post_tool(tool, inp, text, cfg):
    """Side effects only; the output itself passes through untouched."""
    if tool == "multi_edit" and mirror_on():
        _resync(inp.get("tool_input") or {})
    return None


# ---- the old studio agent profile: oof owns the Studio agent now -----------------------------------------------
AGENT_MARK = "<!-- installed by gutcheck (roblox_studio adapter); regenerated on refresh -->"


def remove_agent_profile(dest=None):
    """Remove the studio.md that earlier gutcheck versions installed (it carries AGENT_MARK). A studio.md
    without the mark (the owner's own, or oof's) is never touched. Returns what happened."""
    dest = Path(dest or Path.home() / ".claude" / "agents" / "studio.md")
    try:
        text = dest.read_text()
    except OSError:
        return "absent"
    if AGENT_MARK not in text:
        return "kept (not gutcheck's)"
    dest.unlink()
    return "removed"


def _resync(ti):
    from . import studio
    if any(studio.CACHE.glob("*/.manifest.json")):
        studio.sync(ti.get("studio_id"), quiet=True)


def cli(args):
    if args and args[0] == "preload":
        from . import preload
        return preload.cli(args[1:])
    from . import studio
    if args and args[0] in ("sync", "path", "check") and not mirror_on():
        raise SystemExit("tokenmaxxing: the local Studio mirror is off (experimental). Call the Studio tools directly; "
                         "to use the mirror anyway, set `[studio] mirror = true` in ~/.config/tokenmaxxing/config.toml.")
    studio.main(args)


def rules():
    return (Path(__file__).parent / "rules.md").read_text()
