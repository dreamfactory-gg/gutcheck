---
name: init
description: Build or rebuild ~/.config/gutcheck/config.toml from this user's history.
disable-model-invocation: true
allowed-tools: Bash(*/bin/gutcheck init*)
---
```!
"${CLAUDE_SKILL_DIR}/../../bin/gutcheck" init
```
Above is the output of `gutcheck init`. Tell the user what it set up and anything it needs from them (a missing key, ssh hosts to label in the sensitivity map). Run nothing else unless they ask.
