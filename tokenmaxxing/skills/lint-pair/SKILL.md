---
name: lint-pair
description: Turn copied code or a bypassed helper into one helper plus an ast-grep rule that bans the second copy, scoped to one directory.
argument-hint: "<directory>"
disable-model-invocation: true
---

# Lint pair: one helper, and the rule that makes it the only way

Prose rules get read; lint rules bite. A pair is a helper plus a repo check that fails on a new copy of what it replaced.

1. **Find the copies.** Churn: `git log --since=60.days --name-only --format= -- DIR | sort | uniq -c | sort -rn | head -30`. Then the user's reuse corrections: `tokenmaxxing recall "use the existing helper"` (also "reuse", "duplicate", "copy of"). Look for a function body pasted across files, or an existing helper that code routes around (a hand-rolled list of connections beside an existing cleanup helper).
2. **Defaults, not shape.** Diff the variants. If they differ only in default values, one helper with overrides fits. If they differ in shape (different fields, steps or control flow), stop and tell the user: a parameter per difference is the wrong abstraction.
3. **Make one helper** in the directory it serves. Each file's drifted defaults survive as an explicit preset or override at its call site, so behaviour doesn't change.
4. **Migrate by script**, not by hand. Prove sameness: dump what the old and new code produce (tables, output) and diff.
5. **Ban the second copy with ast-grep.**
   - `sgconfig.yml` at the repo root: `ruleDirs: [<lint dir>]` (`tools/lint` or `scripts/lint`), and for Luau `languageGlobs: { lua: ["*.luau"] }`.
   - One rule file per ban: `severity: error`, a `message` naming the helper, a `note` with its one-line interface, and `files:` (relative to the sgconfig root) scoped to the directory. Leave out generated trees. Allow-list legitimate sites with `not:` or narrower `files:`, never with ignore comments.
6. **Install ast-grep through the repo's own tool manager.** Rokit: `ast-grep = "ast-grep/ast-grep@<version>"` in rokit.toml; setup docs say `rokit trust ast-grep/ast-grep && rokit install` (rokit refuses an untrusted tool without a TTY). npm: `npm i -D @ast-grep/cli` and a `"lint:ast": "ast-grep scan src"` script. Add a step to the repo's check script (with an install hint when ast-grep is missing) and to CI. Always pass the source dir: a bare `ast-grep scan` walks assets for minutes.
7. **Put the interface in the directory's CLAUDE.md** (or the brief its builders read): each function's signature, defaults and a one-line example, and the rule id, so agents call the helper without opening its file.
8. **Prove it**, quoting the three runs: check passes; inject one violation in the scoped directory, check fails naming the rule id; revert, check passes.

Afterwards, more than two ignore comments against a rule means the rule is wrong, not the code.

## Example
Forty Luau files under `src/effects/` each carry a local `emitter` builder, in a dozen drifted default sets.
- Helper `src/shared/Effect.luau`: `Effect.emitter(o)`, and `Effect.preset(base)` for what one file's emitters share (each file's old defaults become its `base`).
- `sgconfig.yml`: `ruleDirs: [tools/lint]`, `languageGlobs: { lua: ["*.luau"] }`. `tools/lint/effect-helpers.yml`:
  ```yaml
  id: effect-helpers
  language: lua
  severity: error
  message: Effects build emitters with shared/Effect.luau, never a local copy.
  note: "local emitter = Effect.preset({ brightness = 2 }); interface in src/effects/CLAUDE.md"
  files: ["src/effects/**"]
  rule:
    any:
      - pattern: local function emitter($$$) $$$ end
      - pattern: local emitter = function($$$) $$$ end
  ```
- `rokit.toml` pins ast-grep; `scripts/check.sh` runs `ast-grep scan src`; `src/effects/CLAUDE.md` holds the Effect.luau interface.
