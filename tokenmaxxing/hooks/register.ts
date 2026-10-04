import type { Register, EngineInterface } from 'claude-code'

// Compact earlier: once the main session's context passes [compact] main_tokens, compact between turns, at most once
// every GAP turns (the gap stops a loop when the prefix plus the re-injected checkpoint sit near the threshold).
// The summary is Claude Code's own; tokenmaxxing's classic PreCompact/SessionStart checkpoint still runs.
// /compact through $.command.run, not $.session.compact(): the latter is refused in headless sessions, and the
// queued command runs once the session is idle, ahead of the next prompt.
const GAP = 3

type Cfg = { mode: 'on' | 'log' | 'off'; main_tokens: number }
let cfg: Cfg = { mode: 'off', main_tokens: 0 }
let turns = GAP

const cli = ($: EngineInterface, ...args: string[]) => $.process.run([`${$.plugin.root}/bin/tokenmaxxing`, ...args])

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    const r = await cli($, 'config', 'compact')
    if (r.exitCode === 0) cfg = JSON.parse(r.stdout)
    return next(e)
  })
  on('turn.complete', async ($, e, next) => {
    const out = await next(e)
    if (e.agentId !== undefined || cfg.mode === 'off' || ++turns < GAP) return out
    const tokens = (await $.session.usage()).context.tokens ?? 0
    if (tokens <= cfg.main_tokens) return out
    turns = 0
    await cli($, 'log', 'compact', JSON.stringify({ mode: cfg.mode, context_tokens: tokens, limit: cfg.main_tokens,
                                                    decision: cfg.mode === 'on' ? 'compact' : 'would-compact' }))
    if (cfg.mode === 'on') {
      $.command.run({ command: 'compact' })
        .catch((err) => cli($, 'log', 'hook_errors', JSON.stringify({ event: 'compact', error: String(err) })))
    }
    return out
  })
}
