# gutcheck

**Claude checks with you before it does something you can't undo.**

gutcheck is a Claude Code plugin that guards against rogue agent actions: risky commands, keys in the transcript, secret files read into the chat, and deleting what you said to keep. It only adds friction; it never approves anything for you. Install and key setup: see the [marketplace README](../README.md); its first session links the `gutcheck` CLI into `~/.local/bin` (manual command there). For longer, cheaper sessions, its sibling plugin is [tokenmaxxing](../tokenmaxxing/README.md).

| When… | Without gutcheck | With gutcheck |
|---|---|---|
| you paste an API key | it's in the transcript for good | the prompt is blocked before it's sent |
| Claude is about to force-push, `rm -rf` or print a secret | it runs if your permissions allow it | Claude has to ask you first |
| Claude writes a key into a command or a file | it's in the transcript and on disk | the call is refused, with a pointer to `gutcheck withsecret` |
| you said "don't delete notes.txt" earlier | a later `rm` or overwrite just runs | Claude has to ask you, quoting what you said |

### Measured

From one maintainer's own Claude Code history on macOS (about two weeks, 2,000+ prompts and 11,000 Bash commands). Your mileage will differ.

- **Pasted keys:** zero false blocks on 2,158 real prompts. Made-up keys of 11 common kinds were all caught.
- **Risky commands:** caught 29 of 30 labelled risky commands, with about 1 false alarm per 200 commands.

## What runs by itself

- **Key blocker.** A prompt you type that contains a known key format is blocked before it's sent. Messages delivered from subagents or other sessions are never blocked; Claude gets a one-line note instead.
- **Secret files.** `.env`, private keys and credential files can't be read into the chat. Templates like `.env.example` stay readable.
- **Don't-delete guard.** When you've told the session not to delete, remove or overwrite something ("don't delete notes.txt", "never overwrite `config/`"; paths that existed when you said it), a command or Write that would (`rm`, `git rm`, `git clean`, `git checkout --`, `git reset --hard`, `git stash drop`, `find -delete`, `mv` or `>` over it, a directory holding it) needs your OK first, quoting what you said.
- **Keys in tool calls.** A literal key in a command, a file Claude writes or edits, or an MCP call is refused, with a pointer to `gutcheck withsecret`. Placeholders, `$VARS` and `.env.example` content pass. A key in a known provider format that a command prints is redacted before Claude sees it.
- **Bash check.** Before a command runs, gutcheck looks for destructive, secret-printing or hidden (decoded-then-run) commands and makes Claude ask you. It starts in logging mode and switches itself on once its own logs show it's fast and quiet enough.
- **House rules before a push.** Before `git push`, the unpushed diff is checked against your CLAUDE.md rules (secrets, conflict markers, "never `X`" literals, then Jev over rule and hunk). Logging only by default; switched on, it asks, never blocks.

## On demand

- `gutcheck withsecret SERVICE ENV -- command`: run a command with a secret in its environment, without the value ever being printed.
- `gutcheck check-bash "COMMAND"`: dry-run the Bash check.
- `gutcheck house-rules [--repo D]`: check the unpushed diff against the project's CLAUDE.md rules.
- `gutcheck report [days]`: Bash asks and approvals, and pasted-key blocks.
- `/gutcheck:init`: build or rebuild your config from your own history (it also runs by itself the first time).

## Safety model

- **It only adds friction.** gutcheck can make Claude ask you, block a pasted key, refuse a tool call holding a key, redact a key from command output, or refuse to read a secret file. It never approves anything for you.
- **It fails open.** If Jev is slow or down, or gutcheck itself errors, your session carries on as if gutcheck weren't there. A hook never blocks Claude Code on its own failure.
- **You get the last word.** If the key blocker is wrong, send the exact same prompt again within 60 seconds and it goes through.
- **What leaves your machine.** Text gutcheck judges goes to TypeSafe's Jev API: the command under review, your prompts (to spot standing instructions) and diff hunks before a push. gutcheck redacts known key formats from command lines before sending or storing them. Everything else stays in `~/.local/state/gutcheck` and the shared standing-rules store, `~/.local/share/gutcheck/rules`.

## Settings and off switches

Everything lives in `~/.config/gutcheck/config.toml`. `gutcheck init` writes it, and it's safe to edit. Each feature has a `mode`:

| Section | What it controls | Default |
|---|---|---|
| `[bash_check] mode` | `log` (record only), `on` (ask), `off` | `log`, switches itself on when healthy (`auto_promote = false` holds it) |
| `[keyblock] mode` | pasted-key blocker | `on` |
| `[key_guard] mode` | keys in tool calls refused, keys in Bash output redacted (`log`: record only) | `on` |
| `[delete_guard] mode` | asks before deleting what you said not to delete (`log`: record only) | `on` |
| `[house_rules] mode` | checks the diff against your CLAUDE.md rules before `git push` | `log` |
| `[sensitivity]` | hosts, branches and projects the Bash check treats as shared or production | `main`, `master` |

To turn everything off, run `claude plugin disable gutcheck@gutcheck`.

## Uninstall

```sh
claude plugin uninstall gutcheck@gutcheck
rm -rf ~/.config/gutcheck ~/.local/state/gutcheck
```

`~/.local/share/gutcheck` holds the venv and the standing-rules store tokenmaxxing also uses; remove it once neither plugin is installed.

## Limitations

- **Logging only for now:** the Bash check (until it switches itself on) and the pre-push house-rules check. They record what they would have done; `gutcheck report` shows it.
- **Tested by one person on one Mac.** Linux is untested and Windows isn't supported.
- **Jev is a judgment model, not a reader.** It's fast and cheap, but weak at counting, arithmetic and dates. gutcheck keeps the hard rules (force push, `rm` outside temp dirs, printed secrets) in code and asks Jev only about the rest.
