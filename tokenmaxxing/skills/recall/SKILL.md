---
name: recall
description: Search past sessions, memory, Codex logs and git history for an earlier decision, fix or approach.
argument-hint: "<question>"
disable-model-invocation: true
allowed-tools: Bash(*/bin/tokenmaxxing recall*)
---

```!
"${CLAUDE_SKILL_DIR}/../../bin/tokenmaxxing" recall "$(cat <<'GUTCHECK_QUESTION'
$ARGUMENTS
GUTCHECK_QUESTION
)"
```

Above is the output of `tokenmaxxing recall` for the user's question. Answer the question from it, citing where each answer came from; say so if it found nothing relevant.
