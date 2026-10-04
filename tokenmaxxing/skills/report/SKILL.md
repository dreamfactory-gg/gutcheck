---
name: report
description: Report what the tokenmaxxing hooks did over the last N days (default 7).
argument-hint: "[days]"
disable-model-invocation: true
allowed-tools: Bash(*/bin/tokenmaxxing report*)
---

```!
"${CLAUDE_SKILL_DIR}/../../bin/tokenmaxxing" report $ARGUMENTS
```

Above is the output of `tokenmaxxing report`: stop checks, stalls, filter and loading decisions, and suggested threshold changes. Show it to the user as-is (a short summary is fine if it is long), point out anything that needs their decision, and run nothing else unless they ask.
