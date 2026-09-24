---
name: report
description: What the gutcheck hooks did (Bash asks and approvals, stop checks, stalls, filter and loading decisions, suggested threshold changes). Optional argument - number of days (default 7).
argument-hint: "[days]"
disable-model-invocation: true
allowed-tools: Bash(*/bin/gutcheck report*)
---

```!
"${CLAUDE_SKILL_DIR}/../../bin/gutcheck" report $ARGUMENTS
```

Above is the output of `gutcheck report`. Show it to the user as-is (a short summary is fine if it is long), point out anything that needs their decision, and run nothing else unless they ask.
