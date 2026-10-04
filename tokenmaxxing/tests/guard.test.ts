import { test, expect } from 'claude-code/testing'
import type { On } from 'claude-code'

// The long-input guard: a stub-shaped value is refused only in `on` mode, once this session recorded a stub.
const STUB = ['[the 2,345', '-char command of this call, run as written; saved at /tmp/x.sh]'].join('')
const run = (stdout: string) => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
const engine = (on: On, mode: string) => {
  on('process.run', ($, e) =>
    run(e.argv[1] === 'config' ? JSON.stringify(e.argv[2] === 'long_input' ? { mode, bash_chars: 2000, write_chars: 4000 } : { mode: 'off' }) : 'null'))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('tool.call', () => ({ result: 'ran' }) as never)
}
const CALLS = [
  ['Bash', { command: `echo '${STUB}'` }],
  ['Write', { file_path: '/w/notes.md', content: `a stub looks like ${STUB}` }],
  ['Edit', { file_path: '/w/t.ts', old_string: `const s = '${STUB}'`, new_string: 'x' }],
  ['Agent', { description: 'grade', prompt: `Check the guard; a stub looks like ${STUB}`, subagent_type: 'general-purpose' }],
  ['SubagentHandback', { message: `Verdict: FAIL. Repro: echo '${STUB}'` }],
] as const

for (const mode of ['log', 'off', 'on']) {
  for (const [tool, input] of CALLS) {
    test(`mode ${mode}, no stub recorded: ${tool} with stub-shaped text runs`, async ($, on) => {
      engine(on, mode)
      await $.session.start({ cwd: '/w', surface: null, isInteractive: false })
      const r = await $.tool.call({ tool, ...input } as never)
      expect(r.deny).toBeUndefined()
    })
  }
}
