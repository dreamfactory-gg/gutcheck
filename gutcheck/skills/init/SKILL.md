---
name: init
description: Build or rebuild the personal gutcheck config (~/.config/gutcheck/config.toml) from this user's own history.
disable-model-invocation: true
allowed-tools: Bash(*/bin/gutcheck init*)
---

```!
"${CLAUDE_SKILL_DIR}/../../bin/gutcheck" init
```

Above is the output of `gutcheck init`. Tell the user what it set up and anything it needs from them; run nothing else unless they ask.
