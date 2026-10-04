---
name: handoff
description: Before a planned restart, /clear or compaction, or on a handoff ask: update TASKS.md `## Now`, move done items to REPORT.md, record running jobs.
---

# Handoff: leave TASKS.md so the next context can carry on

tokenmaxxing re-injects TASKS.md's `## Now` block and open items after a compaction, a `/clear` or a restart (a new session in the same project within 6 hours). Make them current:

1. **Now block**, at the top of TASKS.md (right under the title), exactly:
   ```
   ## Now
   - Goal: <what we're working towards>
   - In progress: <the item being worked on right now>
   - Next step: <the concrete next action>
   - Owner's latest ask: <their most recent request, close to their words>
   - Running jobs: <ids and what each does, or none>
   ```
   It runs to the next heading; keep it short (what gets re-injected is cut at about 1,500 characters). Replace any older Now block, including one marked as written by tokenmaxxing.
2. **Ticked items**: move every `- [x]` item (with its detail lines) to REPORT.md next to TASKS.md, under `## <today> (moved from TASKS.md)`. Never delete or reword an open item.
3. **Running work**: every background job, monitor, cron and live subagent or teammate gets an open item with its id, what it does and where its output goes, e.g. `- [ ] bg job b7x2k1: full test run; output /tmp/…/b7x2k1.output; check it and act on failures`.
4. Tell the owner one line: that TASKS.md is ready and what the next step is.
