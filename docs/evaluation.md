# Evaluation

How the numbers in the READMEs were measured, what they show, and where they stop. Features that missed their bar are listed too, because they explain why some things ship in log mode or not at all.

## Ground rules

- **Data.** Every replay uses one person's own Claude Code history on macOS. Claude Code keeps transcripts for 14 days, so each corpus is a two-week window: 11,147 Bash commands and, depending on the date of the run, 1,971 to 2,158 typed prompts.
- **Labels.** Ground truth was written by a separate model (Claude Opus at high effort) that did not build the feature, unless a section says otherwise. Model labels are not human labels; treat the numbers as strong evidence, not a benchmark.
- **Reproducibility.** The transcripts and labelled sets are personal and are not in this repo, so these numbers cannot be rerun from a clone. What you can run: `tests/` (no key, no network) and `evals/tokens/run.py` (the token-cost suite below).
- **Bars first.** Each feature had a pass bar written before its eval. A feature that missed its bar ships in log mode, where it records what it would have done, or stays off.
- **Jev** is TypeSafe's judgment model: it answers typed questions about a block of text in about a second, at a small fraction of Claude's price. It is weak at counting, arithmetic and dates, so those stay in code.

## gutcheck

### Pasted-key blocker

- **Method.** The key scanner ran over made-up keys of 11 kinds (Anthropic, OpenAI, TypeSafe, Vercel, GitHub, AWS, Slack, Stripe, Google, JWT, PEM private keys) and over benign coding text (git SHAs, sha256 digests, UUIDs, paths, identifiers, base64 of ordinary words). Then it ran over every prompt typed in the transcript window.
- **Result.** All 11 kinds caught; none of the benign text flagged. On 1,971 real prompts: 2 false hits before tuning, 0 after. After the blocker was narrowed to known key formats only (other random-looking strings get a warning, not a block): 0 blocks on 2,158 real prompts.
- **Limits.** Recall is measured on made-up keys only. A wrong block has an escape hatch: the same prompt sent again within 60 seconds goes through.

### Bash command check

- **Corpus.** 11,147 Bash commands mined from transcripts. Wrappers matter: about 10,500 inner commands ran through `ssh`, 3,600 through `$(...)`, 350 through `bash -c`, 313 through `sudo`.
- **Parser.** tree-sitter-bash, about 0.1 ms per command, 0 exceptions on all 11,147. It unwraps `sudo`, `env`, `timeout`, `nohup`, `xargs`, `bash -c`, `ssh host '...'`, `ssh host bash -s <<EOF`, `docker`/`kubectl exec`, shell functions (judged where defined) and variables assigned earlier in the same command.
- **First design, rejected.** Five broad Jev questions over the whole command: recall 29/30, but **78 false alarms in 195 safe commands**. Broad questions over a large mixed state are Jev's documented weak spot.
- **Shipped design.** Deterministic triggers per simple command give the recall; a read-only allowlist settles the common case; Jev only answers narrow questions on a small state (is this kill target a service or this session's own job, does this POST change remote data, do these flagged script lines act on running systems). Python that a command runs is scanned with `ast`.
- **Labelled set A** (225 real commands, stratified; 30 risky, 195 safe): **recall 29/30, false alarms 4/195**, about 1 in 49 safe commands. The miss was an `rm` of an untracked data file; whether it is risky depends on the file existing, and it no longer did at replay time. A live reproduction in a fresh repo asks correctly.
- **Labelled set B** (80 random would-be asks from the full corpus): all 46 true asks still caught; 18 of the 34 unneeded asks remain.
- **Full corpus.** 190 asks in 11,147 commands (1.7%). If set B's share of unneeded asks (18 of 64 remaining, 28%) holds across the corpus, that is about 0.5 unneeded asks per 100 commands. This is an extrapolation, not a measurement.
- **Cost and latency.** 57% of commands are settled by the allowlist with no model call; Jev is consulted on about 2%. End to end, 80 ms for allowlisted commands, plus 0.3 to 1.3 s when Jev is consulted.
- **Firewall finding.** In the first eval run, TypeSafe's web application firewall rejected 13 of the 225 commands outright (HTTP 403 with an HTML page; the gateway route returned 503 for the same payloads). Shell text with ssh, sudo, docker and curl together looks like an attack to a WAF. The check treats a firewall rejection on every route as "couldn't check this command" and asks; only outages and timeouts fail open. The shipped design sends single simple commands or short excerpts, which has not hit the firewall since.
- **Status.** Ships in log mode. It switches itself to asking once its own live log passes a health gate (at least 100 checks, under 2% hook errors, p95 under 3 s, under 5% fail-opens, under 5% would-be asks). The maintainer's install holds it in log mode (`auto_promote = false`), so it has not asked in live use yet.

## tokenmaxxing

### Large MCP output

- **Method.** Replayed 2,600 past MCP calls through the per-tool policy (receipts for action tools, error lines for log tools, outlines or ranges for read tools). On a Roblox Studio test place, planted failures (bad edits, Luau errors, an error buried mid-output, console warnings) were run live.
- **Result.** About half of MCP output stayed out of context. Every failure the agent needed stayed visible: 37 of 37 in the replay, all planted failures live.

### Log triage (`tokenmaxxing jev filter`)

- **Method.** 30 real long outputs from transcripts (test runs, builds, logs, grep results). A labeller wrote the question for each and marked the lines that answer it.
- **Result.** Kept 385 of 6,073 lines (about 6%). Recall 0.939 at `--min 0.5` (precision 0.56); 0.978 at `--min 0.3` (precision 0.43). Misses were bare `--> file:line` continuation lines, now covered by `--context 2`, and a numeric exit code.

### Reading structure before code

- **Method.** `ast-outline` of a file, then `jev filter` over the outline, on 12 "where is X defined" questions.
- **Result.** Right definition kept 12 of 12, top-ranked 12 of 12; the outline is about 16 times smaller than the file. Caveat: the questions were about this repo's own, well-named code.

### Docs routing

- **Method.** Questions routed over a 111-page `llms.txt` docs index.
- **Result.** Right page for 10 of 10 questions, about 2 pages fetched per question (the first version pulled all 910 KB).

### Durable compaction

- **Method.** Replayed all 6 real compactions in the window: build the checkpoint from the transcript as it stood before compaction and check what survives. Then a live headless session: `/compact`, then continue the task.
- **Result.** The first checkpoint, capped at about 5k tokens, kept 33 of 38 standing instructions and 18 of 18 running jobs. Standing instructions now go to their own uncapped section: **38 of 38** survive, at the price of about 1 in 5 non-rule sentences kept as well. Live: 14 of 14 checks passed (instructions respected, open items finished, protected files untouched, running job listed).

### Recall over past sessions

- **Method.** 34 real questions ("how did we fix X", "what did we decide about Y"), quotes checked against the sources.
- **Result.** Right source found for 94%, ranked first for about 3 in 4, answer visible in the returned excerpt for 82 to 88%, in 2 to 4 s. Full-text search alone: 71% and 29%.
- **Seen this error before.** On held-out failures, right 19 of 21 times; it speaks on about 4% of failures. **Already done this task:** fired 9 times on 250 real agent launches, all correct.

### Skill hints

- **Method.** 58 real prompts, plus 13 made-up prompts for specific skills.
- **Result.** The right skill for all 3 prompts where one was used and all 3 where one clearly should have been; no false hints on the other 52. 13 of 13 on the made-up set. Latency p50 about 0.9 s, p95 about 1.2 s. Caveat: 6 positive cases is thin evidence.

### Evidence check on subagent reports

- **Method.** 505 real claims that cite a file, line or command; 128 labelled blind. Compared Jev, Haiku, Sonnet at low effort and a GPT model at low effort.
- **Result.** No classifier cleared the precision bar when "the cited text says nothing about it" counted as unsupported. Flagging only claims the cited text contradicts (plus citations past the end of a file), Sonnet at low effort was right on 6 of 7 flags and catches about half of the unsupported claims. Caveat: that rule was chosen after seeing these labels, and 7 flags is a small number. It adds at most one line of feedback and never blocks.

## Tried and not shipped, or shipped in log mode

- **Jev trimming of MCP output.** Kept only 55% of the lines Claude later used, against a 100% bar. Not shipped; replaced by the per-tool policy above.
- **rtk command rewriting.** On 60 re-run read-only commands it saved 0.9% of output, because the commands already trimmed with `head`, `grep -c` and similar. Not adopted.
- **Long-run stop check.** On 230 real stops the user replied to, it would block about 3 in 5 stalls (bar: 90%) and wrongly block about 1 in 6 legitimate stops. Haiku and a GPT model were tried as alternatives; none reached the bar. Log mode.
- **Code execution with MCP** (the agent writes a script that calls tools and prints only the answer). Every run succeeded in every condition, but it was not lighter than direct calls once tool signatures were complete enough to stop argument errors. Not adopted.
- **Doc and memory section loading.** Picks were plausible, but two model round trips took 1 to 2 s. Log mode until its own log shows p95 under 1 s.

## Release gate: token cost

`evals/tokens/run.py` runs the gated tasks, small made-up jobs such as log triage, one function in a big file, a one-line edit, and large log and JSON output from a fake MCP server, three times each in headless Claude Code, against the candidate build with installed plugins, user settings and memory switched off. A median cost above the task's ceiling blocks the release, and so does a wrong answer beyond the task's tolerance: most tasks allow none, and the two big-file tasks allow one wrong run in three because a low-effort model sometimes misreads them. Cost is gated in cost-equivalent tokens (input + cache writes x1.25 + cache reads x0.1 + output x5), because a Claude Code update can turn cache writes into reads and move the raw counts in opposite directions. Ceilings only move down on their own: after a release, a ceiling drops toward the costliest run plus a noise margin taken from measured run-to-run spread, and only when that run sits on the same cost level as the ceiling. Raising one needs a written reason. A run costs about 30 s and $0.25. The maintainer's ceilings are measured on his machine and not committed; `run.py bootstrap` builds your own.
