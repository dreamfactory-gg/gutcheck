# tokenmaxxing

**Your Claude Code sessions run longer, spend less, and stop asking to continue.**

tokenmaxxing is a Claude Code plugin. It keeps text Claude doesn't need out of its context and holds on to what you told it across compactions. Install and key setup: see the [marketplace README](../README.md); its first session links the `tokenmaxxing` CLI into `~/.local/bin` (manual command there). For guards against risky commands and leaked keys, its sibling plugin is [gutcheck](../gutcheck/README.md).

Every call Claude makes re-sends the whole conversation. Anything that lands in context gets paid for again on every step after it. Long logs, huge tool outputs and forgotten instructions are what make a session expensive and short.

| When… | Without tokenmaxxing | With tokenmaxxing |
|---|---|---|
| a tool returns a huge answer | all of it rides along for the rest of the session | a compact view goes in context, the full output is saved to a file |
| Claude needs one failure from a 2,000-line log | it reads the log | it filters the log first and reads the few lines that matter |
| the session compacts | your standing instructions can get lost in the summary | they are written down before compaction and put back after it |
| a command fails | Claude debugs it from scratch | it's told how the same error was fixed in an earlier session, when there's a confident match |
| a subagent's report cites a file and line | you trust it | the citation is checked, and the subagent hears about a claim its evidence contradicts |
| a turn ends with "want me to continue?" | the work stops | the work keeps going (optional long-run mode) |

### Measured

All numbers come from one maintainer's own Claude Code history on macOS: about two weeks of real sessions, 2,000+ prompts and 11,000 Bash commands. Your mileage will differ.

- **Big tool output:** on 2,600 past MCP calls, about half the output stayed out of context, and every failure the agent needed (37 of 37) stayed visible.
- **Log triage:** on 30 real long outputs, the filter kept about 6% of the lines and still found 94% of the lines that mattered (98% at a looser setting).
- **Docs:** on a 111-page docs site, it found the right page for 10 of 10 questions while fetching about 2 pages per question.
- **Compaction:** replaying 6 real compactions, 38 of 38 standing instructions and 18 of 18 running jobs survived. A live session continued correctly past `/compact` on 14 of 14 checks.
- **Memory of past work:** on 34 real questions ("how did we fix X?"), recall found the right source 94% of the time, in 2–4 seconds.

## What runs by itself, and what Claude reaches for

**Automatic, through hooks:**
- **MCP output.** Large tool answers become a compact view plus a saved file, per tool, based on what Claude actually used from that tool before. Screenshots are never touched.
- **Bash output.** Command output over 4,000 chars can become a compact view with the full output saved to a file. The view includes lines that match what the call was for. Keys in a known format are redacted before the view or the file is written. Commands reading saved outputs stay untouched.

**tokenmaxxing is not a key guard.** On its own it redacts keys only in output it trims; short output, pasted keys, keys Claude writes into commands or files, and secret files pass untouched. Install [gutcheck](../gutcheck/README.md) for key protection.
- **Big file Reads.** A whole-file Read whose first 2,000 lines exceed 30,000 chars is refused once: find the lines with `grep -n`, then Read that range with offset/limit before editing, or repeat the same Read if you need it all. Images, PDFs, notebooks and nearly single-line files stay untouched.
- **Durable transitions.** Before a compaction, a `/clear` or the end of a session, tokenmaxxing writes a checkpoint that starts with "WHERE WE ARE": the `## Now` block from TASKS.md (goal, item in progress, next step, your latest ask, running jobs), then open tasks, your standing instructions word for word, running jobs, files and branches. It goes back in after the compaction or `/clear`, and into a new session in the same project if the last one ended under 6 hours ago and no other session is running there (`--resume` and `--continue` get nothing extra). If the session didn't keep the Now block current (TASKS.md unchanged since your last prompt), tokenmaxxing writes one from the transcript (one `claude -p` turn, Opus at low effort; on exit only when the session keeps a TASKS.md). Ticked items move from TASKS.md to REPORT.md at each transition; open items are never touched. Interactive sessions only: headless `claude -p` runs are skipped. `/tokenmaxxing:handoff` has Claude tidy TASKS.md before a planned restart.
- **Compact earlier** (a mod: `hooks/register.ts`). Once the main session's context passes 400,000 tokens (`[compact] main_tokens`), it is compacted between turns, at most once every 3 turns, by Claude Code's own `/compact`; the checkpoint above goes back in after it. Every later call re-sends the whole context, so a long session spends less when it is compacted before it is huge. Subagents are never compacted this way. If the mod doesn't load, the rest of tokenmaxxing runs as before. It ships in `log` mode: it records in `compact.jsonl` where it would have compacted, and changes nothing until you set `[compact] mode = "on"`.
- **Seen this before.** When a command fails, tokenmaxxing adds one line on how the same error was fixed earlier, but only when it's confident. It does the same when Claude starts a task that was already done.
- **Nudges.** When a long tool loop runs in a large session, tokenmaxxing suggests handing it to a fresh helper agent. A forked agent that would copy more than about 50k tokens of context is refused, with the same suggestion (`[fork_guard] max_context_tokens`).
- **Skill hints.** When a prompt clearly matches an installed skill, Claude gets a one-line pointer to it.
- **Hand-back reports.** Every subagent brief ends with one line: "Start your final report with the verdict and everything your caller must act on (failures, ranked findings, answers); put supporting evidence, logs and long tables under a `## Details` heading in the same report, not in a file: your caller gets the part before it plus the path where the whole report is saved." A report over 2,000 chars with a Details heading (`#` to `###`, any case, outside code fences) reaches the parent as the part before it (at most 3,000 chars, cut at a line) plus the path of the whole report, saved like a trimmed output; a report rides every later call of the parent. A report with no Details heading passes whole, and none is ever refused. Both delivery paths are covered: the SubagentHandback tool (auto mode) and the Agent tool's result (e.g. headless `claude -p`). A background agent's result in headless runs arrives as a task notification and isn't touched.
- **Subagent context cap** (logging only by default). A subagent whose own context passes 250k tokens (`[subagent_cap] tokens`) would get every tool call refused except SubagentHandback, with an ask to write its handoff note and hand back. Main sessions are never capped. It only records `would-deny` by default: measured, 15% of subagents cross 250k mid-task with a median of 49 calls left, and a warm agent resumed past the cap would hand back with no work done. Set `[subagent_cap] mode = "on"` to enforce it.
- **Context size of subagents.** The context-size reader counts subagent transcripts too (it used to skip them), so the soldier reuse check (`[soldier] max_context_tokens`) now sees a resumed agent's real context and suggests retiring it for the first time.
- **Evidence check.** When a subagent finishes, the claims in its report that cite files or commands are checked against those sources.
- **Long-run stop check** (with long-run mode only): a turn that ends with open TASKS.md items gets nudged to continue.

**On demand:**
- `tokenmaxxing jev filter | pick | ask`: triage long text without reading all of it. The `tokenmaxxing:jev` skill teaches Claude when to use it.
- `tokenmaxxing recall "question"`: search your past sessions, memory files, Codex runs and git history.
- `tokenmaxxing skill-find "task"`: the installed skill to read for a task.
- `tokenmaxxing mcp`: what each of your MCP servers adds to every request just by being connected, and how many recent sessions used it. `tokenmaxxing mcp off NAME...` keeps a server off in every project (Claude Code only disables servers per project; tokenmaxxing applies the list to each new project once, so turning one back on with `/mcp` sticks). `tokenmaxxing init` measures it (one short `claude -p` turn per server, about $0.10 each) and asks.
- `tokenmaxxing review`, `relevance`, `evidence`, `explain`: background checks on a finished change (Codex review, test gaps, duplication and complexity, docs drift), a cited-evidence check on any report, and an explainer page.
- Skills: `tokenmaxxing:jev`, `tokenmaxxing:build-units` (plan and check parallel work), `tokenmaxxing:soldiers` (reuse or retire helper agents), `tokenmaxxing:handoff` (tidy TASKS.md before a restart or `/clear`), `/tokenmaxxing:lint-pair DIR` (turn copied code into one helper plus an ast-grep rule that bans the next copy).
- Agents: `tokenmaxxing:explorer` (answers "where is X?" with file and line citations only) and `tokenmaxxing:grader` (an independent check of finished work).
- Slash commands that cost nothing until you type them: `/tokenmaxxing:report [days]`, `/tokenmaxxing:recall QUESTION`, `/tokenmaxxing:init`.

**Optional extras:**
- `ast-outline` and `ast-grep`: Claude can read a map of a big file before reading any of it.
- The Codex CLI, for the background code review and explainer pages (`tokenmaxxing review`, `tokenmaxxing explain`).
- Node (`npx`) or a `jscpd` binary, for the review's duplicate check on changed lines (no model; lizard, for complexity, comes with the shared Python environment).
- zsh long-run mode. Clone this repo and add `source <clone>/tokenmaxxing/shell/long-run.zsh` to `~/.zshrc`. After that, `claude` starts sessions with an instruction to keep working until the job is done, and `short` starts a plain session.

## What leaves your machine

Text tokenmaxxing judges goes to TypeSafe's Jev API: things like log lines you filter, your prompts (to spot standing instructions and skill hints) and excerpts for recall. Known key formats are redacted from reports, excerpts and the history index before sending or storing them. The evidence check sends a subagent's report and its cited lines to Claude (`claude -p`, Sonnet at low effort), which uses a little of your Claude usage. So does the Now-block backup at a transition (the transcript's tail, Opus at low effort), only when the session didn't keep the block current. Everything else, including the history index, stays in `~/.local/state/tokenmaxxing` and the plugin's data dir. It fails open: if Jev is slow or down, or tokenmaxxing errors, your session carries on as if it weren't there.

## Settings and off switches

Everything lives in `~/.config/tokenmaxxing/config.toml`. `tokenmaxxing init` writes it, and it's safe to edit. Each feature has a `mode`:

| Section | What it controls | Default |
|---|---|---|
| `[mcp_trim] mode` | compact views for large MCP output | `on` |
| `[tool_trim] mode` | compact views for long Bash output (`log`: record only, `off`: untouched) | `on` |
| `[read_gate] mode`, `min_chars` | once-only gate on big whole-file Reads (`log`: record only, `off`: untouched) | `on`, `30000` |
| `[stop] mode` | long-run stop check (only with long-run mode) | `log` |
| `[context] mode` | loading the right doc and memory sections per prompt | `log` |
| `[evidence] mode` | subagent citation check | `on` |
| `[skill_router] mode` | skill hints | `on` |
| `[loop_nudge] mode`, `[soldier] mode` | helper-agent nudges | `on` |
| `[handback] mode` | report-shape line in subagent briefs; the part before `## Details` + saved path for long reports (`log`: record only) | `on` |
| `[subagent_cap] mode`, `tokens` | subagents past this context may only hand back (`log`: record `would-deny` only, `on`: enforce) | `log`, `250000` |
| `[compact] mode`, `main_tokens` | compact the main session between turns past this context (`log`: record `would-compact` only) | `log`, `400000` |
| `[transitions] mode` | Now-block backup, ticked-item move, re-inject after `/clear` and restart (`off`: compaction checkpoint only) | `on` |
| `[recall] seen_error`, `[recall] dedup` | "seen this before" hints | `on` |
| `[mcp_servers] default_off` | MCP servers kept off in every project (`tokenmaxxing mcp off NAME`, `on NAME`) | empty |
| `[recall] git_roots` | folders searched for git repos to index | `~/projects`, `~/code`, `~/src` and similar |

To turn off the whole MCP output layer for one session, run `TOKENMAXXING_MCP=off claude`. For one project, add it under `[mcp] off_projects`. To turn everything off, run `claude plugin disable tokenmaxxing@gutcheck`.

`/tokenmaxxing:report` shows what the hooks did: trims, stalls, compactions, MCP waste and savings, and any threshold it suggests changing.

## Uninstall

```sh
claude plugin uninstall tokenmaxxing@gutcheck
rm -rf ~/.config/tokenmaxxing ~/.local/state/tokenmaxxing
```

`~/.local/share/gutcheck` holds the venv and the standing-rules store gutcheck also uses; remove it once neither plugin is installed. If you turned on Roblox Studio tool preloading, ask Claude to run `tokenmaxxing studio preload off` before you uninstall. If you added the long-run line to `~/.zshrc`, remove it. Explainer pages, if you made any, are in `~/Documents/gutcheck-explainers`.

## Limitations and honest status

- **Logging only for now:** the long-run stop check and doc and memory loading. They record what they would have done. `/tokenmaxxing:report` shows it.
- **Tested by one person on one Mac.** Linux is untested and Windows isn't supported.
- **Jev is a judgment model, not a reader.** It's fast and cheap, but weak at counting, arithmetic and dates. tokenmaxxing keeps numbers in code and tells Claude to double-check what Jev flags.
- **The evidence check is young.** It has high precision on a small labelled set and catches about half of unsupported claims.
- **Roblox Studio** has its own adapter. Studio output passes through untouched by default, and the local script mirror is an experiment that stays off unless you turn it on.
- **The compaction checkpoint has no separate off switch yet.** `[transitions] mode = "off"` turns off the rest; disabling the plugin turns off everything.
