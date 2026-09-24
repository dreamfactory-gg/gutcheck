---
name: recall
description: Search past sessions, memory (including archives), Codex logs and git history for an earlier decision, fix or approach.
argument-hint: "<question>"
disable-model-invocation: true
allowed-tools: Bash(*/bin/gutcheck recall*)
---

```!
"${CLAUDE_SKILL_DIR}/../../bin/gutcheck" recall "$(cat <<'GUTCHECK_QUESTION'
$ARGUMENTS
GUTCHECK_QUESTION
)"
```

Above is the output of `gutcheck recall` for the user's question. Answer the question from it, citing where each answer came from; say so if it found nothing relevant.
