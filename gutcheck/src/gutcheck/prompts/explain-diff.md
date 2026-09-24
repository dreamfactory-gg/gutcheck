You are writing a rich, interactive explanation of one code change, as a single self-contained HTML page. The reader owns this project but did not write this change; the page exists so they understand it well enough to have the next idea, not just to trust it. You are read-only: do not modify any file. Your final message must be the complete HTML document and nothing else (start with `<!DOCTYPE html>`, end with `</html>`, no Markdown fences, no commentary).

## Inputs
- Repository: {repo}
- Change: {scope}
{commit_note}
- A relevance pass already selected the files and line ranges around this change (below). Start there; read more surrounding code only where the explanation needs it (`sed -n`, `rg -n`, `git show`, `git log`). Don't re-explore the whole repo.

## Sections (one long page, section headers, a table of contents at the top, no tabs)
1. **Background.** The existing system this change touches. Two layers: a deeper background for a reader new to this area (in a `<details>` element so it can be skipped), then the narrow background directly relevant to the change. Explain what was already there before what changed, and say plainly **why the change was needed**: what went wrong or was missing, and who or what it hurt.
2. **Intuition.** The core idea of the change, the essence rather than every detail. State the goal first. Use concrete examples with toy data, and figures and diagrams liberally.
3. **Code.** A walkthrough of the changes in conceptual order, not file order: group related edits, say why each one is there, and embed the key snippets. Name files and line ranges so the reader can jump to them.
4. **Quiz.** Five multiple-choice questions of medium difficulty, four options each, that you can only answer if you understood the substance of the change: consequences, edge cases, what would happen if an input changed. Not recall of a sentence from the page, no gotchas, no trivia about names. Interactive: clicking an option says whether it's right and gives a one or two sentence explanation either way. Before you finish, check that every option carries whatever attribute your script reads to grade it.

While reading, look for non-obvious behaviour the change creates or leaves in place (a stale value, a path that bypasses the new logic, a retry that multiplies a cost) and point it out in a callout: these are the things the reader most needs.

## Accuracy
- Every statement about the code must be true of the code as changed. Check before you write: read the lines you describe. If something is uncertain, say so in the page rather than guessing.
- Numbers (limits, timeouts, counts, sizes) must be copied from the code, not estimated.
- Don't pad: if the change is small, the page is short.

## Format
- One HTML file with inline CSS and JavaScript, no external resources (no CDNs, fonts or images from the network). Readable on a phone (basic responsive layout), and in light and dark mode (`prefers-color-scheme`).
- Diagrams are simple HTML/CSS (boxes, arrows, tables, small inline SVG), never ASCII art. Pick a small number of diagram families and reuse them: for example a simplified version of the UI the user sees, or a data-flow diagram between components that carries example data.
- Refer to files as plain text (`path:line`), not links to local absolute paths: the page may be read on another device.
- Lists are HTML lists. Code blocks use `<pre>`; if you style a custom block instead it must have `white-space: pre-wrap` (check every code block before you finish).
- Set off the ideas the reader must not miss (a definition, a key concept, a sharp edge case) in visually distinct boxes.
- Prose: plain, concrete and readable, in classic style: show the reader what is true, and let each section lead into the next.

## Relevance pass
{relevance}

## The diff
```diff
{diff}
```
