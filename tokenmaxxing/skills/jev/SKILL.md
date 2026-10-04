---
name: jev
description: Before reading 200+ lines where only part matters (logs, test output, search hits, big files, docs sites): triage with `tokenmaxxing jev` and ast tools.
---

# Reading less: `tokenmaxxing jev`, outlines, docs routing

Jev is a System One model, not an LLM: it answers typed questions over text, costs almost nothing and never enters your context. The point is to **keep text out of your context**. Don't use it for choices you can already make from what you've read.

## Commands
```
tokenmaxxing jev filter "CONDITION" FILE|-  [--by line|block] [--min 0.5] [--context N] [--all]
tokenmaxxing jev pick "QUESTION" "name: description" ... [--state FILE|-]
tokenmaxxing jev ask STATE_FILE '{"q1": {"type": "noul", "instructions": "..."}}'
ast-outline FILE            # signatures + line ranges, no bodies (~16x smaller than the file)
ast-outline show FILE NAME  # source of one symbol
ast-grep -p 'PATTERN' -l LANG PATH   # structural search (calls, definitions), exact
```
`filter` prints `lineno<TAB>p<TAB>text` for matching items (stderr has the count). `--by block` splits on blank lines (stack traces, multi-line records). `--context N` adds surrounding lines.

## Default approaches
1. **Search → filter → read.** `rg -n`/`ast-grep` for candidates → `tokenmaxxing jev filter` over the hits → read only what's left.
2. **Big files: structure before reading.** `ast-outline FILE | tokenmaxxing jev filter "Is this line the definition that answers: <question>?"` → `ast-outline show FILE NAME` or Read just that line range. (Measured: right function kept 12/12, ~1 of 33 outline lines kept.)
3. **Docs sites.** Fetch `llms.txt` (or the site index), then `tokenmaxxing jev filter "Would the page described in this index line directly help answer: <question>?"` and fetch only the kept pages. (Measured on a 111-page index: recall 10/10 questions, ~2 pages kept.)
4. **Tests and builds: quiet first.** Run failures-only (`cargo test -q 2>&1 | grep -E "^test .* FAILED|panicked|^error"`, `pytest -q -x --no-header -rf`, `npm test -- --silent`), then pipe anything still long through `filter "Does this line report a failure, error, panic, crash, failed assertion or fatal condition (not a warning, not a passing result)?" --min 0.3 --context 2` (measured on 30 real outputs: recall 0.98 keeping ~6% of lines; the misses were bare `--> file:line` continuation lines, which `--context` covers, and numeric exit codes, which you should check in code). Tables of per-target results are parsed in code, not by Jev.
5. **Codex runs and subagent reports.** Filter long run logs before reading them.
6. **Exploration across many files:** hand it to the `explorer` agent, which returns `path:line-range` citations only, then read just those ranges.
7. **Past work: `tokenmaxxing recall "QUESTION"`** (what did we decide, how did we fix or approach X) searches past sessions, memory (including `memory/archive/`), Codex runs and git history, and prints short excerpts with `path:line` pointers. `tokenmaxxing recall --at PATH:LINE` shows the indexed excerpt at a pointer (don't open the whole transcript); `tokenmaxxing recall --error "ERROR LINE"` finds how an error was handled before. (Measured on 34 real questions: the right source among the results for 94%, the answer visible in the excerpt for 85%.)
8. **Screenshots:** read them whole. Crop or zoom (`sips -c H W --cropOffset Y X in.png --out crop.png`, or the browser tool's region/zoom) only for dense detail such as small text or technical drawings.

## Writing questions (Jev reads literally)
- One exact, self-contained condition per question. Name the thing; put boundary cases in the question ("not a warning").
- No arithmetic, counting, date comparison or numeric thresholds: do those in code; ask Jev about each item and count the answers yourself.
- No multi-hop ("is the caller of X deprecated?"). Split into literal questions and combine in code.
- Keep the state small and relevant: filter first. Don't rely on Jev for security judgments about adversarial text.

## Confidence gating
- `filter`: keep `--min 0.5` for recall-sensitive work (a missed failure costs more than an extra line); use 0.3 when a miss is expensive.
- `pick` returns `confidence`: below ~0.5, or top two probabilities within 0.15, read both candidates yourself.
- Zero matches where you expected some: re-run with `--all` or a broader condition before concluding it isn't there.

## Verify what Jev flags
Jev narrows; you decide. Read the flagged lines before acting on them, and never report a Jev result as fact without checking it.

## Never send secrets
Don't send `.env` contents, keys, tokens, credentials or private keys to Jev, Codex or any third-party model. Strip them first, or don't send the file.
