import type { Register, EngineInterface } from 'claude-code'

// Compact earlier: once the main session's context passes [compact] main_tokens, compact between turns, at most once
// every GAP turns (the gap stops a loop when the prefix plus the re-injected checkpoint sit near the threshold).
// The summary is Claude Code's own; tokenmaxxing's classic PreCompact/SessionStart checkpoint still runs.
// /compact through $.command.run, not $.session.compact(): the latter is refused in headless sessions, and the
// queued command runs once the session is idle, ahead of the next prompt.
const GAP = 3

type Mode = 'on' | 'log' | 'off'
let compact: { mode: Mode; main_tokens: number } = { mode: 'off', main_tokens: 0 }
let turns = GAP
// Fixed prefix: decided at session start and never changed after (a changed attachment re-writes the cache).
let prefix: { mode: Mode } = { mode: 'off' }
let usedServers: Set<string> | undefined   // MCP server slugs called in the last 30 days; unknown before a refresh

const cli = ($: EngineInterface, args: string[]) => $.process.run([`${$.plugin.root}/bin/tokenmaxxing`, ...args])
const json = async ($: EngineInterface, args: string[]) => {
  const r = await cli($, args)
  return r.exitCode === 0 ? JSON.parse(r.stdout) : undefined
}
const log = ($: EngineInterface, name: string, rec: object) => cli($, ['log', name, JSON.stringify(rec)])

const slug = (name: string) => name.replace(/[^A-Za-z0-9_-]/g, '_')

// The text the model reads for a prefix attachment, or null to leave it out.
const prefixText = (type: string, text: string, agentId: string | undefined): string | null => {
  if (type === 'total_tokens_reminder') return null
  if (type === 'skill_listing') return agentId === undefined ? text : null   // subagents almost never load a skill
  if (type === 'mcp_instructions_delta' && usedServers) {
    return text.split(/^(?=## )/m)
      .filter((block) => !block.startsWith('## ') || usedServers!.has(slug(block.slice(3, block.indexOf('\n')).trim())))
      .join('')
  }
  return text
}

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    compact = (await json($, ['config', 'compact'])) ?? compact
    prefix = (await json($, ['config', 'prefix'])) ?? prefix
    const used: string[] | null | undefined = await json($, ['mcp', 'used'])
    usedServers = used ? new Set(used) : undefined
    return next(e)
  })

  on('prompt.attachment', async ($, e, next) => {
    const out = await next(e)
    if (prefix.mode === 'off' || out.text === null) return out
    const text = prefixText(e.type, out.text, e.agentId)
    if (text === out.text) return out
    await log($, 'prefix', { mode: prefix.mode, type: e.type, agent: e.agentId ?? null, chars: out.text.length,
                             out_chars: text?.length ?? 0 })
    return prefix.mode === 'on' ? { text } : out
  })

  on('turn.complete', async ($, e, next) => {
    const out = await next(e)
    if (e.agentId !== undefined || compact.mode === 'off' || ++turns < GAP) return out
    const tokens = (await $.session.usage()).context.tokens ?? 0
    if (tokens <= compact.main_tokens) return out
    turns = 0
    await log($, 'compact', { mode: compact.mode, context_tokens: tokens, limit: compact.main_tokens,
                              decision: compact.mode === 'on' ? 'compact' : 'would-compact' })
    if (compact.mode === 'on') {
      $.command.run({ command: 'compact' })
        .catch((err) => log($, 'hook_errors', { event: 'compact', error: String(err) }))
    }
    return out
  })
}
