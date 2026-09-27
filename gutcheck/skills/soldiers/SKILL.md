---
name: soldiers
description: Reuse a helper subagent across a long tool loop instead of starting fresh each time, and retire it with a handoff note when it's large, off-topic or cold. Use when handing a long tool-heavy loop (e.g. Studio, a browser, a debugger) to a subagent, or before resuming one.
---

# Persistent soldiers: reuse while warm, retire with a note

A soldier is a subagent you keep for a stretch of related work so its context (what it has read, what it learned) isn't rebuilt on every hand-off. Your judgment decides when to hand off at all; direct calls are the default.

**Start.** Agent tool with a `name` (e.g. "studio") and a short brief: goal, ids and paths, how to verify, what you've ruled out. It replies with conclusions only.

**Reuse** it with SendMessage (`to` = its name) while all three hold:
- the work stays in the same area (same scripts, feature or bug);
- it's warm: it answered within the last hour (the subagent prompt cache TTL; past it a resume re-reads its whole context at full price);
- its context is still small.

**Retire** it when its context is large, the topic changes or it has gone cold. SendMessage it:
> retire: reply with your handoff note only. First line exactly `HANDOFF NOTE`, then under 30 lines: what you learned, the files (or scripts) and line ranges involved, what's done and what's open.

gutcheck saves that reply as the project's note for that agent type, and the next agent of the same type in this project gets it at start automatically (once). Then start the replacement with your brief as usual. `gutcheck soldier-note [AGENT_TYPE]` prints the latest note (`--path` for its location).

**Two costs to weigh.** A resume via SendMessage runs the agent in the background, so the main session spends about two extra turns per hand-off (wait, notification, result); a fresh launch in the foreground returns within the turn. And a subagent's prompt cache lasts 5 minutes by default (an hour only with `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL=1h` or a profile's `experimental: {cacheTtl: 1h}`), after which a resume re-sends its whole context at full price. Reuse pays only when the next piece of work is close, related and needs what the agent already read.

**Per-type policies.** **Studio** (oof's eval): call Studio directly while the main context is under ~100k; above that, start a fresh studio agent in the foreground for each Studio task and don't resume it. gutcheck adds one line when a Studio call from a large main session should go to an agent, and one line if you try to resume a studio agent.

**Thresholds.** When you resume an agent past them, gutcheck adds one line suggesting retirement; it never blocks. They're in `~/.config/gutcheck/config.toml` under `[soldier]` (`max_context_tokens`, `max_idle_minutes`), per agent name or type under `[soldier.agents.<name>]`. The idle default follows the subagent cache lifetime (4 minutes, or 55 with the 1-hour setting); the size default is a placeholder.
