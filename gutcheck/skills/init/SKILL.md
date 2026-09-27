---
name: init
description: Build or rebuild the personal gutcheck config (~/.config/gutcheck/config.toml) from this user's own history.
disable-model-invocation: true
allowed-tools: Bash(*/bin/gutcheck init*), Bash(*/bin/gutcheck mcp*)
---

```!
"${CLAUDE_SKILL_DIR}/../../bin/gutcheck" init
```

Above is the output of `gutcheck init`. Tell the user what it set up and anything it needs from them. Its last table is what each MCP server adds to every request and how many recent sessions used it: ask the user (one multi-select question, suggested servers first) which to keep off by default, then run `gutcheck mcp off NAME...` for those (`gutcheck mcp on NAME...` undoes it). Run nothing else unless they ask.
