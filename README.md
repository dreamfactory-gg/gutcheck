# gutcheck and tokenmaxxing

Two Claude Code plugins that make an agent safer to leave running on real work: **gutcheck** keeps keys, secret files and irreversible actions under the owner's control, and **tokenmaxxing** keeps the context window lean so long sessions stay cheap and on task.

Built by [Eaven Portillo](https://www.linkedin.com/in/eaven-portillo).

## Status

- **Daily use, one person, one Mac.** Built and run every day on macOS for several weeks. Linux should work but is untried; Windows isn't supported.
- **What enforces today:** the pasted-key blocker, refusal of keys written into commands or files, redaction of keys in command output, the secret-file block on direct Read and Grep, and the don't-delete guard.
- **What runs in log mode:** the Bash command check records what it would have asked about, and switches itself to asking once its own live log passes a health gate. On the author's install it is held in log mode, so it has not yet asked in live use. Several tokenmaxxing features (earlier compaction, the subagent context cap, the long-run stop check) also ship in log mode.
- **Needs** Python 3.12+ and a [TypeSafe](https://typesafe.ai) or Vercel AI Gateway key for the judgment-model features. Without a key those stay off and the plugin says so at session start.
- **No CI yet.** Tests and the token-cost release gate run locally.

## Why it exists

An agent doing real work holds real keys, runs commands on real servers and fills its context with logs until it forgets what it was told. Permission prompts alone don't fix that: either you approve everything and stop reading, or you allow everything and hope. These plugins sit in Claude Code's hook points and cover the four things a working agent needs a harness for:

| Problem | What the plugins do |
|---|---|
| **Secrets** | A typed prompt with a key in a known format is blocked before it's sent. A key written into a command, file or MCP call is refused with a pointer to `gutcheck withsecret`, which hands the secret to the command through its environment so it never appears in the command text (what the command itself prints is up to the command). Read can't open `.env` or key files, and neither can a Grep pointed at one; a Grep across a whole directory, or a read through Bash while the Bash check is in log mode, isn't blocked. |
| **Irreversible actions** | "Don't delete notes.txt", said once, still holds twenty turns later. Force pushes, deletes outside temp dirs, secret-printing and decoded-then-run commands are found by a shell parser that unwraps `ssh`, `sudo`, `bash -c`, heredocs and `docker exec` (log mode by default; see Status). |
| **Context** | Long tool output becomes a compact view plus a saved file. Standing instructions and running jobs are written down before a compaction and put back after it. A failed command gets one line on how the same error was fixed before. |
| **Review** | Claims in a subagent's report that cite a file and line are checked against that file. On demand, background checks on a finished change cover tests, duplication and docs drift. |

## What each plugin does

**[gutcheck](gutcheck/README.md)** guards against rogue agent actions. It only adds friction: it can make Claude ask, block a prompt, refuse a call or redact output, and it never approves anything for you.

- Pasted-key blocker for known key formats, with a 60-second resend override for a wrong block.
- Secret-file block on Read, and on Grep pointed at the file, for `.env`, private keys and credential files; `.env.example` stays readable.
- Key guard on Bash, Write, Edit and MCP calls; known key formats redacted from command output.
- Don't-delete guard: your own words become protected paths, and `rm`, `git clean`, `git reset --hard`, `mv` or `>` over them needs your OK.
- Bash command check (log mode by default) and a pre-push check of the unpushed diff against your CLAUDE.md rules (log mode by default).

**[tokenmaxxing](tokenmaxxing/README.md)** makes sessions run longer and cost less. Every call re-sends the whole conversation, so anything that enters context is paid for again on every later step.

- Compact views of large MCP and Bash output, chosen per tool from what Claude actually used before; the full output is saved to a file.
- A once-only gate on huge whole-file Reads, so Claude greps for the lines it needs first.
- Durable transitions: a checkpoint before every compaction, `/clear` and session end, put back afterwards.
- `tokenmaxxing recall`: search over past sessions, memory, Codex runs and git history.
- Hand-back shaping: a subagent's long report reaches the parent as its summary plus the path of the full report.

The plugins install separately, share a small Python core and one venv, and read one store of standing instructions.

## How it's built

- **Hard rules in code, a cheap model for narrow judgments.** A hook runs on every command, so it has to answer in about a second and cost almost nothing. The shell parser (tree-sitter-bash, 0.1 ms per command) and a read-only allowlist settle 57% of commands with no model call. TypeSafe's Jev, a fast judgment model, is asked only narrow questions on small inputs, such as "is this kill target a service or this session's own job". The first design asked broad questions about whole commands and raised 78 false alarms on 195 safe ones; the narrow design raised 4.
- **Fail open on outages, ask on uncertainty.** If the judge is down, slow or erroring, or a hook crashes, the session continues as if the plugin weren't installed: a broken guard should not stop your work. A firewall rejecting the command text is different: the command couldn't be checked, so Claude asks. That case came from a real eval, where TypeSafe's firewall rejected 13 of 225 commands.
- **Shadow mode before enforcement.** Anything that judges ships logging what it would do. It is promoted only when a replay against real history or its own live log clears a bar written in advance. Self-tuning is conservative: a command pattern is approved only after you approve it three times (never for secret-printing or hidden commands), and thresholds never loosen on their own.
- **Measured on replays of real work.** Each feature was evaluated against two weeks of real transcripts with independently written labels before it shipped. Several features missed their bar and were left off or kept in log mode; those results are in the evaluation too.
- **Releases gated on token cost.** A small suite runs headless Claude Code tasks against the candidate build. A wrong answer beyond a task's tolerance, or a median cost above a ceiling that only ratchets down, blocks the release.

## Measured, not claimed

From replays of one person's Claude Code history: 11,147 Bash commands and about 2,000 typed prompts over two weeks. Method, labels, caveats and the features that failed their bars are in **[docs/evaluation.md](docs/evaluation.md)**.

| What | Result |
|---|---|
| Pasted-key blocker, false blocks | 0 on 2,158 real prompts; made-up keys of 11 kinds all caught |
| Bash check, labelled set of 225 commands | 29 of 30 risky commands caught; 4 false alarms in 195 safe ones (1 in 49) |
| Bash check, latency | 80 ms for allowlisted commands; 0.3 to 1.3 s more when the judge is asked |
| Large MCP output, 2,600 past calls | about half kept out of context; 37 of 37 needed failures still visible |
| Log triage, 30 real long outputs | 6% of lines kept, 94% of the lines that mattered found |
| Compaction, 6 real compactions replayed | 38 of 38 standing instructions and 18 of 18 running jobs kept |
| Recall, 34 real questions | right source for 94%, in 2 to 4 s |

## Install

You need Claude Code, Python 3.12 or newer (with [uv](https://docs.astral.sh/uv/) installed, the plugins fetch it themselves), and a TypeSafe or Vercel AI Gateway key for the judgment features.

```sh
claude plugin marketplace add dreamfactory-gg/gutcheck
claude plugin install gutcheck@gutcheck
claude plugin install tokenmaxxing@gutcheck
```

Store the key where the plugins can find it. You'll be prompted for the value, so it never lands in your shell history:

```sh
security add-generic-password -a "$USER" -s typesafe -w      # macOS Keychain
secret-tool store --label=typesafe service typesafe          # Linux
```

Use the service name `ai-gateway` for a Vercel AI Gateway key, or set `GUTCHECK_SECRET_TYPESAFE` or `GUTCHECK_SECRET_AI_GATEWAY` in the environment Claude Code starts from.

On first start, each plugin builds the shared venv (`~/.local/share/gutcheck/venv`), writes its config (`~/.config/gutcheck/config.toml`, `~/.config/tokenmaxxing/config.toml`) and links its CLI into `~/.local/bin` when that directory exists and the name is free. To link it by hand:

```sh
ln -s ~/.claude/plugins/cache/gutcheck/gutcheck/<version>/bin/gutcheck ~/.local/bin/gutcheck
ln -s ~/.claude/plugins/cache/gutcheck/tokenmaxxing/<version>/bin/tokenmaxxing ~/.local/bin/tokenmaxxing
```

## How the two plugins share work

- Each has its own hooks, skills, CLI, config and state (`~/.local/state/gutcheck`, `~/.local/state/tokenmaxxing`). They share the `gutcheck_core` package (config, Jev client, key scanning, shell parsing, transcript reading, hook plumbing) and one venv.
- Standing instructions you give a session ("don't delete notes.txt", "never push to main") are captured once into a store both read (`~/.local/share/gutcheck/rules`). gutcheck's don't-delete guard and tokenmaxxing's compaction checkpoint both use it.
- Claude Code runs both plugins' Bash output hooks at once and can't order them, so tokenmaxxing redacts keys itself before it builds a view or saves a file. Whichever finishes last wins, and both versions are redacted.
- A prompt with a key in it is blocked by gutcheck; tokenmaxxing never sends such a prompt to Jev or the rules store.

## Development

```sh
for t in tests/test_*.py; do ~/.local/share/gutcheck/venv/bin/python "$t" || break; done   # no key, no network, no personal data
python3 evals/tokens/run.py measure --plugin tokenmaxxing --runs 1    # token suite: small made-up tasks in headless Claude Code
```

Layout: `gutcheck/` and `tokenmaxxing/` are the plugins; `core/` holds the shared Python package, `setup.sh` and the Python finder, linked into each plugin (an install copies them in); `evals/tokens/` is the release gate's suite. The token suite spends a little real Claude usage.

Issues and pull requests are welcome. The most useful reports are a case where gutcheck let a risky action through or got in your way, and a case where tokenmaxxing let something expensive into context.

## License

MIT. See [LICENSE](LICENSE).
