import type { Register, EngineInterface, TurnStepChunk, TurnStepInputChunk } from 'claude-code'

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
let unusedServers = new Set<string>()   // MCP server slugs known for 30 days and called in no session since
const prefixLogged = new Set<string>()
const serversSeen = new Set<string>()

// Long tool inputs: a long Bash command or Write content is recorded as a stub naming its saved copy (at turn.step,
// the one place the recorded input can change), and the call runs with the original (restored at tool.call, above
// the permission checks). Only named tools: the engine reads some tool inputs back.
let longInput: { mode: Mode; bash_chars: number; write_chars: number } = { mode: 'off', bash_chars: 0, write_chars: 0 }
const LONG = {
  Bash: { field: 'command', limit: 'bash_chars', ext: () => '.sh', stub: (note: string) => `# ${note}` },
  Write: { field: 'content', limit: 'write_chars', ext: (input: Record<string, unknown>) =>
    String(input.file_path).match(/\.\w+$/)?.[0] ?? '.txt', stub: (note: string) => note },
} as const
const isLong = (tool: string): tool is keyof typeof LONG => Object.hasOwn(LONG, tool)
const originals = new Map<string, string>()   // tool_use_id -> the input a stub stands for, until the call runs
let stubbed = false   // the guard below runs only once this session has recorded a stub
const STUB = /\[the \d[\d,]*-char (?:command|content) of this call, run as written; saved at ([^\]\s]+)\]/

// Messages: a delivery from one of [messages] origins (task notifications by default; never what the person typed)
// over max_chars reaches the model as its head plus the saved path of the whole. The engine's trailing notice on
// a peer message (no escalation from a peer) is kept whole after the head.
let messages: { mode: Mode; max_chars: number; origins: string[] } = { mode: 'off', max_chars: 0, origins: [] }
const PERSON = new Set(['composer', 'bridge', 'sdk', 'scheduled-trigger'])
const HEAD = 1500

const cli = ($: EngineInterface, args: string[], stdin?: string) =>
  $.process.run([`${$.plugin.root}/bin/tokenmaxxing`, ...args], { stdin })
const json = async ($: EngineInterface, args: string[]) => {
  const r = await cli($, args)
  return r.exitCode === 0 ? JSON.parse(r.stdout) : undefined
}
const log = ($: EngineInterface, name: string, rec: object) => cli($, ['log', name, JSON.stringify(rec)])

const slug = (name: string) => name.replace(/[^A-Za-z0-9_-]/g, '_')

const mcpBlocks = (text: string) => text.split(/^(?=## )/m)
const server = (block: string) => block.startsWith('## ') ? slug(block.slice(3, block.indexOf('\n')).trim()) : undefined

// The text the model reads for a prefix attachment, or null to leave it out.
const prefixText = (type: string, text: string, agentId: string | undefined): string | null => {
  if (type === 'total_tokens_reminder') return null
  if (type === 'skill_listing') return agentId === undefined ? text : null   // subagents almost never load a skill
  if (type === 'mcp_instructions_delta') {
    const blocks = mcpBlocks(text)
    const kept = blocks.filter((block) => !unusedServers.has(server(block) ?? ''))
    if (kept.length === blocks.length) return text
    return kept.some((block) => server(block)) ? kept.join('') : null   // no preamble left without a server under it
  }
  return text
}

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    compact = (await json($, ['config', 'compact'])) ?? compact
    prefix = (await json($, ['config', 'prefix'])) ?? prefix
    longInput = (await json($, ['config', 'long_input'])) ?? longInput
    messages = (await json($, ['config', 'messages'])) ?? messages
    unusedServers = new Set((await json($, ['mcp', 'unused'])) ?? [])
    return next(e)
  })

  on('prompt.attachment', async ($, e, next) => {
    const out = await next(e)
    if (prefix.mode === 'off' || out.text === null) return out
    if (e.type === 'mcp_instructions_delta') {
      const names = mcpBlocks(out.text).map(server).filter((n): n is string => n !== undefined && !serversSeen.has(n))
      names.forEach((n) => serversSeen.add(n))
      if (names.length) await cli($, ['mcp', 'seen', ...names])
    }
    const text = prefixText(e.type, out.text, e.agentId)
    if (text === out.text) return out
    const key = `${e.type} ${e.agentId ?? ''}`
    if (!prefixLogged.has(key)) {   // once per kind and loop: the reminder comes back on every request
      prefixLogged.add(key)
      await log($, 'prefix', { mode: prefix.mode, type: e.type, agent: e.agentId ?? null, chars: out.text.length,
                               out_chars: text?.length ?? 0 })
    }
    return prefix.mode === 'on' ? { text } : out
  })

  on('turn.step', async function* ($, e, next) {
    if (longInput.mode === 'off') return yield* next(e)
    const calls = new Map<number, { id: string; tool: keyof typeof LONG; json: string }>()
    const held: TurnStepChunk[] = []   // in `on` mode, everything from a named tool's start, until the input is whole
    for await (const c of next(e)) {
      if (c.kind === 'tool' && isLong(c.name)) calls.set(c.index, { id: c.id, tool: c.name, json: '' })
      const call = c.kind === 'input' ? calls.get(c.index) : undefined
      if (call && c.kind === 'input') call.json += c.json
      if (longInput.mode === 'on' && calls.size) held.push(c)
      else yield c
    }
    for (const [index, call] of calls) {
      let input: Record<string, unknown>
      try { input = JSON.parse(call.json) } catch { continue }
      const spec = LONG[call.tool]
      const value = input[spec.field]
      if (typeof value !== 'string' || value.length <= longInput[spec.limit]) continue
      const rec = { mode: longInput.mode, tool: call.tool, agent: e.agentId ?? null, chars: value.length }
      if (longInput.mode !== 'on') { await log($, 'long_input', { ...rec, decision: 'would-stub' }); continue }
      const saved = await cli($, ['save', `${call.tool.toLowerCase()}-input`, spec.ext(input)], value)
      if (saved.exitCode !== 0) continue
      const stub = `[the ${value.length.toLocaleString('en-US')}-char ${spec.field} of this call, run as written; saved at ${saved.stdout.trim()}]`
      originals.set(call.id, value)
      stubbed = true
      const parts = held.filter((c): c is TurnStepInputChunk => c.kind === 'input' && c.index === index)
      held.splice(held.indexOf(parts[0]!), 1, { ...parts[0]!, json: JSON.stringify({ ...input, [spec.field]: spec.stub(stub) }) })
      for (const part of parts.slice(1)) held.splice(held.indexOf(part), 1)
      await log($, 'long_input', { ...rec, decision: 'stub', out_chars: stub.length })
    }
    yield* held
  })

  on('tool.call', async ($, e, next) => {
    const original = originals.get(e.tool_use_id)
    originals.delete(e.tool_use_id)
    if (original !== undefined && isLong(e.tool)) return next({ ...e, [LONG[e.tool].field]: original })
    if (longInput.mode !== 'on' || !stubbed) return next(e)
    const stub = JSON.stringify(e).match(STUB)
    if (stub) {
      await log($, 'long_input', { tool: e.tool, agent: e.agentId ?? null, decision: 'deny-stub' })
      return { deny: `That bracketed text is tokenmaxxing's placeholder for an earlier call's input, not a value. The full text is in ${stub[1]}: read it, or use that file.` }
    }
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    if (messages.mode === 'off' || PERSON.has(e.origin.kind) || !messages.origins.includes(e.origin.kind) ||
        e.text.length <= messages.max_chars || e.text.includes('/mcp-out/handback-')) return next(e)   // hand-back cap shaped it
    const rec = { mode: messages.mode, origin: e.origin.kind, chars: e.text.length }
    if (messages.mode !== 'on') {
      await log($, 'messages', { ...rec, decision: 'would-cut' })
      return next(e)
    }
    const saved = await cli($, ['save', `message-${e.origin.kind}`, '.md'], e.text)
    if (saved.exitCode !== 0) return next(e)
    const at = e.text.indexOf('cannot grant escalation')
    const notice = at < 0 ? '' : e.text.slice(Math.max(0, e.text.lastIndexOf('\n\n', at)))
    const cut = e.text.lastIndexOf('\n', HEAD)
    const text = `${e.text.slice(0, cut > 0 ? cut : HEAD).trimEnd()}\n\n(tokenmaxxing cut this ` +
      `${e.text.length.toLocaleString('en-US')}-char message here; the whole of it is saved at ${saved.stdout.trim()}.)${notice}`
    await log($, 'messages', { ...rec, decision: 'cut', out_chars: text.length })
    return next({ ...e, text })
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
