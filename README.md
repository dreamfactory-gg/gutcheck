# gutcheck

**Your Claude Code sessions run longer, spend less, and stop asking to continue.**

gutcheck is a Claude Code plugin. It keeps text Claude doesn't need out of its context, holds on to what you told it across compactions, and makes Claude check with you before a risky command runs. You keep working the way you do now.

---

## What changes for you

Every call Claude makes re-sends the whole conversation. Anything that lands in context gets paid for again on every step after it. Long logs, huge tool outputs and forgotten instructions are what make a session expensive and short.

| When… | Without gutcheck | With gutcheck |
|---|---|---|
| a tool returns a huge answer | all of it rides along for the rest of the session | a compact view goes in context, the full output is saved to a file |
| Claude needs one failure from a 2,000-line log | it reads the log | it filters the log first and reads the few lines that matter |
| the session compacts | your standing instructions can get lost in the summary | they are written down before compaction and put back after it |
| a command fails | Claude debugs it from scratch | it's told how the same error was fixed in an earlier session, when there's a confident match |
| you paste an API key | it's in the transcript for good | the prompt is blocked before it's sent |
| Claude is about to force-push, `rm -rf` or print a secret | it runs if your permissions allow it | Claude has to ask you first |
| a subagent's report cites a file and line | you trust it | the citation is checked, and the subagent hears about a claim its evidence contradicts |
| a turn ends with "want me to continue?" | the work stops | the work keeps going (optional long-run mode) |

### Measured

All numbers come from one maintainer's own Claude Code history on macOS: about two weeks of real sessions, 2,000+ prompts and 11,000 Bash commands. Your mileage will differ.

- **Big tool output:** on 2,600 past MCP calls, about half the output stayed out of context, and every failure the agent needed (37 of 37) stayed visible.
- **Log triage:** on 30 real long outputs, the filter kept about 6% of the lines and still found 94% of the lines that mattered (98% at a looser setting).
- **Docs:** on a 111-page docs site, it found the right page for 10 of 10 questions while fetching about 2 pages per question.
- **Compaction:** replaying 6 real compactions, 38 of 38 standing instructions and 18 of 18 running jobs survived. A live session continued correctly past `/compact` on 14 of 14 checks.
- **Memory of past work:** on 34 real questions ("how did we fix X?"), recall found the right source 94% of the time, in 2–4 seconds.
- **Pasted keys:** zero false blocks on 2,158 real prompts. Made-up keys of 11 common kinds were all caught.
- **Risky commands:** caught 29 of 30 labelled risky commands, with about 1 false alarm per 200 commands.

## Install

**You need:**
- **Claude Code**
- **Python 3.12 or newer.** If you have [uv](https://docs.astral.sh/uv/), gutcheck fetches the right Python by itself.
- **macOS.** That's where it's built and tested. Linux should work (keys via `secret-tool` or environment variables) but hasn't been tried. Windows isn't supported: the launchers are POSIX shell scripts.
- **A [TypeSafe](https://typesafe.ai) API key, or a Vercel AI Gateway key.** gutcheck's quick yes/no judgments run on TypeSafe's Jev model, which costs a small fraction of what Claude does. Without a key, the Jev-backed features stay off and gutcheck tells you so at session start.

**Then:**

```sh
claude plugin marketplace add dreamfactory-gg/gutcheck
claude plugin install gutcheck@gutcheck
```

Store your key where gutcheck can find it. You'll be prompted for the value, so it never lands in your shell history:

```sh
security add-generic-password -a "$USER" -s typesafe -w      # macOS Keychain
secret-tool store --label=typesafe service typesafe          # Linux
```

Use the service name `ai-gateway` for a Vercel AI Gateway key. You can also set `GUTCHECK_SECRET_TYPESAFE` or `GUTCHECK_SECRET_AI_GATEWAY` in the environment Claude Code starts from.

Start Claude Code. `gutcheck init` does the rest: it sets up its Python environment, reads your own session history on your machine, and writes your personal config to `~/.config/gutcheck/config.toml`. It runs by itself in the background the first time. To see what it found, type `/gutcheck:init` in a session.

**Optional extras:**
- `ast-outline` and `ast-grep`: Claude can read a map of a big file before reading any of it.
- The Codex CLI, for the background code review and explainer pages (`gutcheck review`, `gutcheck explain`).
- Node (`npx`) or a `jscpd` binary, for the review's duplicate check on changed lines (no model; lizard, for complexity, comes with gutcheck's Python environment).
- zsh long-run mode. Clone this repo and add `source <clone>/gutcheck/shell/long-run.zsh` to `~/.zshrc`. After that, `claude` starts sessions with an instruction to keep working until the job is done, and `short` starts a plain session.

## What runs by itself, and what Claude reaches for

**Automatic, through hooks:**
- **Key blocker.** A prompt that contains a known key format is blocked before it's sent.
- **Secret files.** `.env`, private keys and credential files can't be read into the chat. Templates like `.env.example` stay readable.
- **Bash check.** Before a command runs, gutcheck looks for destructive, secret-printing or hidden (decoded-then-run) commands and makes Claude ask you. It starts in logging mode and switches itself on once its own logs show it's fast and quiet enough.
- **MCP output.** Large tool answers become a compact view plus a saved file, per tool, based on what Claude actually used from that tool before. Screenshots are never touched.
- **Durable compaction.** Before a compaction, gutcheck writes a checkpoint: your standing instructions word for word, open tasks, running jobs, files and branches. After compaction, it goes back in.
- **Seen this before.** When a command fails, gutcheck adds one line on how the same error was fixed earlier, but only when it's confident. It does the same when Claude starts a task that was already done.
- **Nudges.** When a long tool loop runs in a large session, gutcheck suggests handing it to a fresh helper agent. A forked agent that would copy more than about 50k tokens of context is refused, with the same suggestion (`[fork_guard] max_context_tokens`).
- **Skill hints.** When a prompt clearly matches an installed skill, Claude gets a one-line pointer to it.
- **Evidence check.** When a subagent finishes, the claims in its report that cite files or commands are checked against those sources.

**On demand:**
- `gutcheck jev filter | pick | ask`: triage long text without reading all of it. The `gutcheck:jev` skill teaches Claude when to use it.
- `gutcheck recall "question"`: search your past sessions, memory files, Codex runs and git history.
- `gutcheck mcp`: what each of your MCP servers adds to every request just by being connected, and how many recent sessions used it. `gutcheck mcp off NAME...` keeps a server off in every project (Claude Code only disables servers per project; gutcheck applies the list to each new project once, so turning one back on with `/mcp` sticks). `gutcheck init` measures it (one short `claude -p` turn per server, about $0.10 each) and asks.
- `gutcheck withsecret SERVICE ENV -- command`: run a command with a secret in its environment, without the value ever being printed.
- Skills: `gutcheck:jev`, `gutcheck:build-units` (plan and check parallel work), `gutcheck:soldiers` (reuse or retire helper agents).
- Agents: `gutcheck:explorer` (answers "where is X?" with file and line citations only) and `gutcheck:grader` (an independent check of finished work).
- Slash commands that cost nothing until you type them: `/gutcheck:report [days]`, `/gutcheck:recall QUESTION`, `/gutcheck:init`.

## Safety model

- **It only adds friction.** gutcheck can make Claude ask you, block a pasted key, refuse to read a secret file, or refuse a fork that would copy a large context. It never approves anything for you.
- **It fails open.** If Jev is slow or down, or gutcheck itself errors, your session carries on as if gutcheck weren't there. A hook never blocks Claude Code on its own failure.
- **Keys stay out of the chat.** Pasted keys are blocked, secret files can't be read, and commands that would print a secret need your OK. `withsecret` hands a key to a command without showing it.
- **You get the last word.** If the key blocker is wrong, send the exact same prompt again within 60 seconds and it goes through.
- **What leaves your machine.** Text gutcheck judges goes to TypeSafe's Jev API: things like the command under review, log lines you filter, your prompts (to spot standing instructions) and excerpts for recall. gutcheck redacts known key formats from command lines, reports, excerpts and its history index before sending or storing them. The evidence check sends a subagent's report and its cited lines to Claude (`claude -p`, Sonnet at low effort), which uses a little of your Claude usage. Everything else, including the history index, stays in `~/.local/state/gutcheck`.

## Settings and off switches

Everything lives in `~/.config/gutcheck/config.toml`. `gutcheck init` writes it, and it's safe to edit. Each feature has a `mode`:

| Section | What it controls | Default |
|---|---|---|
| `[bash_check] mode` | `log` (record only), `on` (ask), `off` | `log`, switches itself on when healthy (`auto_promote = false` holds it) |
| `[keyblock] mode` | pasted-key blocker | `on` |
| `[mcp_trim] mode` | compact views for large MCP output | `on` |
| `[stop] mode` | long-run stop check (only with long-run mode) | `log` |
| `[context] mode` | loading the right doc and memory sections per prompt | `log` |
| `[evidence] mode` | subagent citation check | `on` |
| `[house_rules] mode` | checks the diff against your CLAUDE.md rules before `git push` | `log` |
| `[skill_router] mode` | skill hints | `on` |
| `[loop_nudge] mode`, `[soldier] mode` | helper-agent nudges | `on` |
| `[recall] seen_error`, `[recall] dedup` | "seen this before" hints | `on` |
| `[mcp_servers] default_off` | MCP servers kept off in every project (`gutcheck mcp off NAME`, `on NAME`) | empty |
| `[recall] git_roots` | folders searched for git repos to index | `~/projects`, `~/code`, `~/src` and similar |

To turn off the whole MCP output layer for one session, run `GUTCHECK_MCP=off claude`. For one project, add it under `[mcp] off_projects`. To turn everything off, run `claude plugin disable gutcheck@gutcheck`.

`/gutcheck:report` shows what the hooks did: asks, blocks, trims, stalls, and any threshold it suggests changing.

## Uninstall

```sh
claude plugin uninstall gutcheck@gutcheck
claude plugin marketplace remove gutcheck
rm -rf ~/.config/gutcheck ~/.local/state/gutcheck ~/.local/share/gutcheck
```

If you turned on Roblox Studio tool preloading, ask Claude to run `gutcheck studio preload off` before you uninstall. If you added the long-run line to `~/.zshrc`, remove it. Explainer pages, if you made any, are in `~/Documents/gutcheck-explainers`.

## Limitations and honest status

- **Logging only for now:** the Bash check (until it switches itself on), the long-run stop check, doc and memory loading, and the pre-push house-rules check. They record what they would have done. `/gutcheck:report` shows it.
- **Tested by one person on one Mac.** The numbers above are from one person's history. Linux is untested and Windows isn't supported.
- **Jev is a judgment model, not a reader.** It's fast and cheap, but weak at counting, arithmetic and dates. gutcheck keeps numbers in code and tells Claude to double-check what Jev flags.
- **The evidence check is young.** It has high precision on a small labelled set and catches about half of unsupported claims.
- **Roblox Studio** has its own adapter. Studio output passes through untouched by default, and the local script mirror is an experiment that stays off unless you turn it on.
- **Durable compaction has no separate off switch yet.** Disabling the plugin turns it off.

## Contributing

Issues and pull requests are welcome. The best thing you can send is a case where gutcheck let something expensive into context, or got in your way.

```sh
for t in tests/test_*.py; do python3 "$t" || break; done      # unit tests; no key, no network, no personal data
python3 evals/tokens/run.py measure --plugin gutcheck --runs 1  # token suite: small made-up tasks in headless Claude Code
```

The token suite spends a little real Claude usage. It needs Python 3.12+ and the `claude` CLI.

## License

MIT. See [LICENSE](LICENSE).
