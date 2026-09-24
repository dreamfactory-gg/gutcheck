"""Studio soldiers: no reuse; fresh agent per task above ~100k main context. General soldiers unchanged. Made-up data."""
import json, os, sys, tempfile
from pathlib import Path
d = Path(tempfile.mkdtemp())
os.environ["GUTCHECK_STATE_DIR"] = str(d)
os.environ["GUTCHECK_CONFIG_DIR"] = str(d / "cfg")
(d / "cfg").mkdir()
(d / "cfg" / "config.toml").write_text('adapters = ["roblox_studio"]\n')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gutcheck", "src"))
from gutcheck import soldier  # noqa: E402
from gutcheck.adapters import roblox_studio as rs  # noqa: E402

assert soldier.policy("studio", "studio")["reuse"] is False
assert soldier.policy("builder", "general-purpose")["reuse"] is True
os.environ.pop("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL", None)
assert soldier.thresholds("builder", "general-purpose")["max_idle_minutes"] == 4      # 5-min subagent cache
os.environ["CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL"] = "1h"
assert soldier.thresholds("builder", "general-purpose")["max_idle_minutes"] == 55     # 1-hour cache
os.environ.pop("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL")
assert soldier.policy("builder", "general-purpose")["reuse"] is True          # general reuse unchanged

# resuming a studio agent: always told to start fresh instead
main = d / "s.jsonl"
main.write_text("")
sub = d / "s" / "subagents"
sub.mkdir(parents=True)
(sub / "agent-abc123.meta.json").write_text(json.dumps({"name": "studio", "agentType": "studio"}))
(sub / "agent-abc123.jsonl").write_text("")
out = soldier.check_send({"transcript_path": str(main), "tool_input": {"to": "studio", "message": "next task"}})
assert out and "don't resume" in out["additionalContext"], out

# main-context hint: silent below 100k, once at/above it, never inside a subagent
assert rs.studio_hint({}, 60_000, False) is None
assert rs.studio_hint({}, 120_000, False).startswith(rs.STUDIO_NUDGE_MARK)
assert rs.studio_hint({}, 120_000, True) is None
assert rs.studio_hint({"agent_id": "x"}, 500_000, False) is None
print("studio soldier policy ok")
