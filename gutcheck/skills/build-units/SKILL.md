---
name: build-units
description: Plan, parallelise and finish units of build work. Concrete done bars in TASKS.md, parallel builders in worktrees that state their files first, and when a unit is done, background checks that only read (Codex review, test-gap, docs-drift), an independent grader, evidence checks on reports, and an explainer page. Use when splitting work across builders or when a unit is finished.
---

# Build units: done bars, parallel builders, background checks

## A concrete done bar for every goal (TASKS.md)
Each open item carries a bar that a stranger could check without asking you: a command and its expected result, a measurement and its threshold, or an observable behaviour.
```
- [ ] CSV export — done when: `pytest tests/test_export.py` passes and a 10k-row export takes < 2 s
- [ ] Retry on 429 — done when: the fake-server test shows 3 retries with backoff, and no retry on 400
```
Not a bar: "works", "clean", "fast", "robust". If the right measure isn't obvious, design one before building (a fixture, an eval over real data, a timing). The long-run loop and the grader both work against this bar.

## Parallel builders don't clobber each other
- Give each parallel builder its own worktree by default (`isolation: "worktree"` on the Agent call). Share one checkout only for read-only agents.
- The brief asks the builder to **state the files it will write before it starts**, as its first line of output. Compare the lists across builders; if two overlap, serialise those two or split the file's ownership explicitly.
- A builder that finds it needs an unlisted file says so before touching it.
- Merge worktrees one at a time; run the linters and type checkers after each merge.
- Testing: unit tests only for small, crucial pieces (a green unit suite often hides code that doesn't work). A unit's done bar is something an end-to-end run on the real thing shows (real app, real Studio, real server, real logs); prove it that way and report what the run showed, not a test count.

## When a reviewable unit is done
Everything here only reads, runs in the background, and never gates. Start it, keep working, read the results when they land.

1. **Background review** (Codex, adversarial; plus test-gap and docs-drift from the same relevance pass):
   ```
   gutcheck review --wait "<what to attack: the risky part, the invariant that must hold>" [--commit SHA | --base REF]
   ```
   Run that with Bash `run_in_background: true` so you're notified when it lands, or use `--background`, which detaches and prints the job dir. Results: `<job>/SUMMARY.md` (one line per check) → `review.md`, `test-gap.md`, `docs-drift.md`. `gutcheck review --status` lists recent jobs. Treat findings as leads: verify each against the code before acting.
2. **Shared relevance pass.** `gutcheck relevance [--commit SHA]` selects the changed symbols and the callers, tests and docs that matter, once, with Jev, and saves it (`~/.local/state/gutcheck/relevance/`). Review, test-gap, docs-drift and the explainer all reuse it; hand the `.md` path to any subagent that needs context for the change instead of letting it explore.
3. **Independent grader.** Spawn the `grader` agent (fresh context; never the builder, never yourself for your own build) with: the done bar, where the real output is, and how to run it. It tries to prove the unit fails and reports PASS/FAIL with reproductions. It doesn't fix anything; fixes go back to a builder.
4. **Evidence in reports.** The SubagentStop hook checks each claim in a subagent's report that cites `path:line` or a command it ran, and gives the subagent one line if a citation doesn't back its claim. For Codex output or any other report: `gutcheck evidence REPORT.md --cwd REPO [--transcript JSONL]`. Flags are leads, not verdicts: read the cited lines before rejecting a claim.
5. **Explainer** for substantial units (a new mechanism, a design change, anything the owner will build on; not for small fixes):
   ```
   gutcheck explain --background [--commit SHA | --base REF] [--focus "what the owner should come away understanding"]
   ```
   Codex writes one self-contained HTML page (background, intuition with toy data and diagrams, code walkthrough, 5-question quiz) to `~/Documents/gutcheck-explainers/YYYY-MM-DD-<slug>.html`, outside the repo. Link it from REPORT.md and the end-of-run notification. If the owner wants it on their phone or shared, offer to publish it as an artifact.

## Before `git push`
The house-rules hook checks the unpushed diff against the project's CLAUDE.md rules (secrets, conflict markers, "never `X`" literals, then Jev over rule x hunk). It logs by default; when switched on it asks, never blocks. Dry run: `gutcheck house-rules [--base REF]`.

## Reports teach
A unit's report says what was done **and** how the system works now, in plain language, so the owner can steer the next step. When the owner is struggling with a system, offer a small interactive tool (a step-through view, a side-by-side panel) instead of more prose.
