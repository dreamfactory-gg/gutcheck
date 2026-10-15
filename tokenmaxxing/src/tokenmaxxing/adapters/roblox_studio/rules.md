## Roblox Studio calls
- Batch independent Studio calls in one turn (several tool calls in one message); fold several checks into one `execute_luau` that prints one compact line per check.
- Never poll: wait once (`wait_job_finished`, or a `task.wait(n)` at the top of the checking `execute_luau`), then check once.
- `script_grep`, then a ranged `script_read` (start/end lines), not the whole script. `search_game_tree`: pass `max_depth` 2-3, `head_limit` and `path`.
- `execute_luau`: short scripts; print compact results (counts, key fields, the first few items), never whole tables or trees.
- Main context above ~100k: hand each Studio task to a fresh studio agent in the foreground (short brief, conclusion back); don't resume it for the next task (a resume runs in the background and costs extra main turns). Below that, call Studio directly.
