# gutcheck and tokenmaxxing

Two Claude Code plugins from one marketplace. Install either one, or both.

- **[gutcheck](gutcheck/README.md)** stops rogue agent actions. Claude has to ask you before a risky command runs, pasted and literal keys are blocked, secret files stay out of the chat, and "don't delete X" holds.
- **[tokenmaxxing](tokenmaxxing/README.md)** makes sessions run longer and cost less. It keeps text Claude doesn't need out of its context, holds on to what you told it across compactions, and finds how you solved something before.

You keep working the way you do now. Neither plugin needs the other.

## Install

**You need:**
- **Claude Code**
- **Python 3.12 or newer.** If you have [uv](https://docs.astral.sh/uv/), the plugins fetch the right Python by themselves.
- **macOS.** That's where they're built and tested. Linux should work (keys via `secret-tool` or environment variables) but hasn't been tried. Windows isn't supported: the launchers are POSIX shell scripts.
- **A [TypeSafe](https://typesafe.ai) API key, or a Vercel AI Gateway key.** Both plugins make quick yes/no judgments with TypeSafe's Jev model, which costs a small fraction of what Claude does. Without a key, the Jev-backed features stay off and each plugin tells you so at session start.

**Then:**

```sh
claude plugin marketplace add dreamfactory-gg/gutcheck
claude plugin install gutcheck@gutcheck
claude plugin install tokenmaxxing@gutcheck
```

Store your key where the plugins can find it. You'll be prompted for the value, so it never lands in your shell history:

```sh
security add-generic-password -a "$USER" -s typesafe -w      # macOS Keychain
secret-tool store --label=typesafe service typesafe          # Linux
```

Use the service name `ai-gateway` for a Vercel AI Gateway key. You can also set `GUTCHECK_SECRET_TYPESAFE` or `GUTCHECK_SECRET_AI_GATEWAY` in the environment Claude Code starts from.

Start Claude Code. Each plugin sets up the Python environment they share (`~/.local/share/gutcheck/venv`) and writes its own config the first time: `~/.config/gutcheck/config.toml` and `~/.config/tokenmaxxing/config.toml`.

## How they work together

- Each plugin has its own hooks, skills, CLI (`gutcheck`, `tokenmaxxing`), config and state (`~/.local/state/gutcheck`, `~/.local/state/tokenmaxxing`). They share a small Python core (config, Jev client, key scanning, shell-command analysis, transcript reading, hook plumbing) and one venv.
- The standing instructions you give a session ("don't delete notes.txt", "never push to main") are captured once into a store both plugins read (`~/.local/share/gutcheck/rules`). gutcheck's don't-delete guard and tokenmaxxing's compaction checkpoint both use it.
- **Bash output.** gutcheck redacts keys in a known format from command output; tokenmaxxing turns long output into a compact view and saves the full output to a file. Claude Code runs the two hooks at the same time and can't order them, so tokenmaxxing redacts keys itself before it builds a view or saves a file. With both installed, a long output that holds a key can be rewritten by both; the one that finishes last wins, and both versions are redacted.
- A prompt you type with a key in it is blocked by gutcheck. tokenmaxxing leaves such a prompt alone: it never goes to Jev or the rules store.

## Contributing

Issues and pull requests are welcome. The best things you can send are a case where gutcheck let a risky action through or got in your way, and a case where tokenmaxxing let something expensive into context.

```sh
for t in tests/test_*.py; do ~/.local/share/gutcheck/venv/bin/python "$t" || break; done   # no key, no network, no personal data
python3 evals/tokens/run.py measure --plugin tokenmaxxing --runs 1    # token suite: small made-up tasks in headless Claude Code
```

Layout: `gutcheck/` and `tokenmaxxing/` are the plugins; `core/` holds the shared Python package (`gutcheck_core`), `setup.sh` and the Python finder, linked into each plugin (an install copies them in). The token suite spends a little real Claude usage. It needs Python 3.12+ and the `claude` CLI.

## License

MIT. See [LICENSE](LICENSE).
