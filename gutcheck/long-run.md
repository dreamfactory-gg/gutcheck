A standing instruction from the user about when to end a turn. A reply without a tool call ends your turn, and nothing more happens until the user writes again. So while work the user asked for is still unfinished, keep going instead of stopping. In particular, don't end a turn by:
- summarising what you did and naming the next step without starting it;
- offering to continue, or asking whether you should, when the next step is routine;
- listing decisions for the user when none of them blocks the rest of the work;
- stopping to report because a milestone is done or the turn has been long.
Progress notes and recommendations are welcome: put them in the same message as your next tool call, and keep working on whatever doesn't depend on the answer. If you catch yourself inviting the user to redirect you or offering to wait, drop that line and do the next thing.
Stop when nothing can move without the user (a credential, a manual step, a decision that blocks everything left), when what's blocking you is deliberately protected from you, or when the work is done. This never overrides asking first before risky or destructive actions.

Keep the task list in TASKS.md; tick items as you finish them.
