---
name: init
description: Build or rebuild ~/.config/tokenmaxxing/config.toml and the skill, doc and recall indexes from this user's history.
disable-model-invocation: true
allowed-tools: Bash(*/bin/tokenmaxxing init*), Bash(*/bin/tokenmaxxing mcp*)
---

```!
"${CLAUDE_SKILL_DIR}/../../bin/tokenmaxxing" init
```

Above is the output of `tokenmaxxing init`. Tell the user what it set up and anything it needs from them. Its last table is what each MCP server adds to every request and how many recent sessions used it: ask the user (one multi-select question, suggested servers first) which to keep off by default, then run `tokenmaxxing mcp off NAME...` for those (`tokenmaxxing mcp on NAME...` undoes it). Run nothing else unless they ask.
