"""Roblox Studio adapter. Studio calls go direct and come back untouched: the core MCP output layer passes
Roblox_Studio through (config `mcp_trim.pass_through`), with no receipts, outlines or saved files; oof, when
installed, shapes Studio output at the source. What the adapter still does:
  - the Studio rules: rules.md, and the marked block in Roblox projects' CLAUDE.md (install_rules);
  - removing the `studio` agent profile earlier versions installed (oof owns the Studio agent now);
  - preloading the Studio tools in Roblox projects instead of deferring them behind ToolSearch (preload.py);
  - the experimental local script mirror (studio.py): `gutcheck studio sync` and the resync after an edit, both off
    unless `[studio] mirror = true`."""
from pathlib import Path

SERVER = "Roblox_Studio"
# oof's eval: Studio work moves to a fresh foreground agent per task once the main context is above ~100k (below
# that, call Studio directly), and a studio agent is never resumed: a SendMessage resume runs in the background and
# costs the main session ~2 extra turns per hand-off, which a warm, same-area reuse arm still lost on.
SOLDIER_POLICIES = {r"studio": {"reuse": False, "fresh_above_main_tokens": 100_000}}
STUDIO_NUDGE_MARK = "gutcheck: main context is"


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
    from ... import config
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
        raise SystemExit("gutcheck: the local Studio mirror is off (experimental). Call the Studio tools directly; "
                         "to use the mirror anyway, set `[studio] mirror = true` in ~/.config/gutcheck/config.toml.")
    studio.main(args)


def rules():
    return (Path(__file__).parent / "rules.md").read_text()


BLOCK_START, BLOCK_END = "<!-- gutcheck:roblox-studio -->", "<!-- /gutcheck:roblox-studio -->"
HANDOFF_RULE = ("- A long Studio loop may go to a subagent, your call (oof's studio agent when installed): reuse it via "
                "SendMessage while it's on the same area and warm; retire it with a handoff note when it's large, "
                "off-topic or cold (skill `gutcheck:soldiers`).")


def project_rules():
    """The marked block for Roblox projects' CLAUDE.md: rules.md plus the hand-off rule, under one heading."""
    body = [l for l in rules().strip().splitlines() if not l.startswith("#")]
    return "\n".join(["## Roblox Studio MCP (gutcheck)", "- Call the Studio tools directly; output comes back untouched, "
                      "so ask only for what you need."] + body + [HANDOFF_RULE])


def install_rules(projects, backup_root=None, dry_run=False, refresh_only=False):
    """Add (or refresh) a marked rules block in each project's CLAUDE.md, backing the file up first. Only the
    marked block changes; refresh_only leaves projects without a block alone."""
    import shutil
    from ... import config
    backup_root = Path(backup_root or config.STATE_DIR / "backups" / "project-files")
    done = []
    for proj in projects:
        d = Path(proj)
        if not d.is_dir() or d == Path.home() / "projects" or d == Path.home():
            continue
        f = d / "CLAUDE.md"
        if not f.exists() and _tracked_in_git(d, "CLAUDE.md"):
            continue  # the owner deleted a tracked CLAUDE.md in the working tree; don't recreate it
        text = f.read_text() if f.exists() else ""
        if refresh_only and BLOCK_START not in text:
            continue
        block = f"{BLOCK_START}\n{project_rules()}\n{BLOCK_END}"
        if BLOCK_START in text:
            new = text[:text.index(BLOCK_START)] + block + text[text.index(BLOCK_END) + len(BLOCK_END):]
        else:
            new = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
        if new == text:
            continue
        if not dry_run:
            if f.exists():
                b = backup_root / d.name
                b.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, b / "CLAUDE.md.before-studio-block")  # never over other tools' CLAUDE.md backups
            f.write_text(new)
        done.append(str(f))
    return done


def _tracked_in_git(d, name):
    import subprocess
    r = subprocess.run(["git", "-C", str(d), "ls-files", "--error-unmatch", name], capture_output=True)
    return r.returncode == 0
