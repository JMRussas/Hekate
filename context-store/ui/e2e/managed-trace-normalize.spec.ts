// Node-side tests of the attempt-trace normalizer (trace contract v0, rev 2). No browser.
//
// The Codex fixture is the byte-exact events.jsonl of the standalone Codex smoke-004 (root msg
// 2448, sha256 e946aab8…). Claude records follow tests/fake_cli.py's stream-json shapes.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import type { TraceRecordView } from '../src/planContract/types';
import { normalizeTrace, REASONING_OMITTED } from '../src/components/managed/traceNormalize';
import { summarizeAttempts } from '../src/components/managed/traceState';
import { event } from './managed-helpers';

const CODEX_FIXTURE = new URL('./fixtures/codex-smoke-004.events.jsonl', import.meta.url);

function records(lines: (string | object)[], stream = 'stdout'): TraceRecordView[] {
  return lines.map((l, seq) => ({ seq, tMs: seq * 10, stream, text: typeof l === 'string' ? l : JSON.stringify(l), cut: false, redacted: false }));
}
const roles = (items: { role: string }[]) => items.map(i => i.role);

test('the Codex fixture is the root-verified smoke-004 output', () => {
  const sha = createHash('sha256').update(readFileSync(CODEX_FIXTURE)).digest('hex');
  expect(sha).toBe('e946aab825c6084212dee1a8204211b6a2c8d9c65a8ea59d7fbd44de50b8acec');
});

test('Codex smoke-004: one call and one paired result per command, each agent message once', () => {
  const lines = readFileSync(CODEX_FIXTURE, 'utf8').split('\n').filter(l => l !== '');
  const items = normalizeTrace(records(lines), 'codex-cli');
  expect(roles(items)).toEqual(['system', 'system', 'assistant', 'tool_call', 'tool_result', 'assistant', 'result']);
  const call = items[3];
  const result = items[4];
  expect(call.toolId).toBe('item_1');
  expect(call.toolName).toBe('command_execution');
  expect(call.text).toContain('Get-Content -LiteralPath README.md');
  expect(result.toolId).toBe('item_1');
  expect(result.isError).toBe(false);
  expect(result.text).toContain('[exit 0]');
  expect(result.text).toContain('ChatAgent â€”');   // PowerShell 5.1 mojibake is shown as delivered
  expect(items[6].text).toContain('output_tokens');
});

test('Codex: a completed item without a seen start gives its call too; failures are errors', () => {
  const items = normalizeTrace(records([
    { type: 'item.completed', item: { id: 'i9', type: 'command_execution', command: 'npm test', aggregated_output: 'boom', exit_code: 1, status: 'failed' } },
    { type: 'item.started', item: { id: 'm1', type: 'agent_message', text: '' } },
    { type: 'item.completed', item: { id: 'm1', type: 'agent_message', text: 'done' } },
    { type: 'turn.failed', error: { message: 'limit' } },
  ]), 'codex-cli');
  expect(roles(items)).toEqual(['tool_call', 'tool_result', 'assistant', 'result']);
  expect(items[1].isError).toBe(true);
  expect(items[3].isError).toBe(true);
});

test('Codex reasoning that reaches the browser is not displayed, only noted', () => {
  const items = normalizeTrace(records([{ type: 'item.completed', item: { id: 'r', type: 'reasoning', text: 'secret plan' } }]), 'codex-cli');
  expect(items).toEqual([expect.objectContaining({ role: 'note', text: REASONING_OMITTED })]);
});

test('Claude (fake-cli shapes): init, text, paired tool use and result, result; partial events skipped', () => {
  const items = normalizeTrace(records([
    { type: 'system', subtype: 'init', model: 'sonnet', cwd: 'wt-r1', tools: ['Read', 'Edit'] },
    { type: 'stream_event', event: { type: 'content_block_delta' } },
    { type: 'assistant', message: { content: [{ type: 'text', text: 'Reading.' }, { type: 'tool_use', id: 't1', name: 'Read', input: { file_path: 'README.md' } }] } },
    { type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 't1', content: [{ type: 'text', text: '# ChatAgent' }] }] } },
    { type: 'user', message: { content: [{ type: 'text', text: 'HEKATE-ACT {}' }] } },
    { type: 'result', subtype: 'success', is_error: false, num_turns: 7, total_cost_usd: 0.05, duration_ms: 15184 },
  ]), 'fake-cli');
  expect(roles(items)).toEqual(['system', 'assistant', 'tool_call', 'tool_result', 'raw', 'result']);
  expect(items[0].text).toBe('init · model sonnet · cwd wt-r1 · 2 tools');
  expect(items[2]).toMatchObject({ toolName: 'Read', toolId: 't1', text: '{"file_path":"README.md"}' });
  expect(items[3]).toMatchObject({ toolName: 'Read', toolId: 't1', text: '# ChatAgent', isError: false });
  expect(items[5].text).toBe('success · 7 turns · $0.05 · 15184 ms');
});

test('a redacted record keeps its visible text and tool items, then notes the omission once', () => {
  const [r] = records([{ type: 'assistant', message: { content: [{ type: 'text', text: 'Plan:' }, { type: 'tool_use', id: 't2', name: 'Edit', input: {} }] } }]);
  const items = normalizeTrace([{ ...r, redacted: true }], 'claude-cli');
  expect(roles(items)).toEqual(['assistant', 'tool_call', 'note']);
  expect(items[2].text).toBe(REASONING_OMITTED);
  // An unfiltered thinking block is never displayed either, and the note is not repeated.
  const [t] = records([{ type: 'assistant', message: { content: [{ type: 'thinking', thinking: 'secret' }, { type: 'text', text: 'ok' }] } }]);
  const both = normalizeTrace([{ ...t, redacted: true }], 'claude-cli');
  expect(roles(both)).toEqual(['note', 'assistant']);
  expect(both.some(i => i.text.includes('secret'))).toBe(false);
});

test('stderr, supervisor notes, non-JSON lines, cut lines and unknown kinds', () => {
  const items = normalizeTrace([
    { seq: 0, tMs: 0, stream: 'stderr', text: 'CreateProcessAsUserW failed: Access is denied', cut: false, redacted: false },
    { seq: 1, tMs: 1, stream: 'stdout', text: 'not json', cut: false, redacted: false },
    { seq: 2, tMs: 2, stream: 'stderr', text: 'x'.repeat(10), cut: true, redacted: false },
    { seq: 3, tMs: 3, stream: 'hekate', text: 'killed:inactivity', cut: false, redacted: false },
    { seq: 4, tMs: 4, stream: 'hekate', text: 'trace_incomplete:journal_error', cut: false, redacted: false },
    { seq: 5, tMs: 5, stream: 'hekate', text: 'exit:1', cut: false, redacted: false },
  ], 'claude-cli');
  expect(roles(items)).toEqual(['stderr', 'raw', 'stderr', 'note', 'note', 'note']);
  expect(items[2].cut).toBe(true);
  expect(items.slice(3).map(i => i.text)).toEqual([
    'Supervisor stopped the worker (inactivity).', 'Trace incomplete (journal_error).', 'Worker exited with code 1.']);
  const unknown = normalizeTrace(records([{ type: 'assistant', message: { content: [] } }]), 'other-cli');
  expect(roles(unknown)).toEqual(['raw']);
});

test('attempts are grouped by attemptId from node events, with their latest decision', () => {
  const a1 = { ...event(1, 'n'), attemptId: 'pilot-r1' };
  const a1done = { ...event(2, 'n', 'attempt_finished'), attemptId: 'pilot-r1' };
  const a1dec = { ...event(3, 'n', 'decision_recorded'), attemptId: 'pilot-r1', decision: 'rejected' };
  const a2 = { ...event(4, 'n'), attemptId: 'pilot-r2', attemptEpoch: 2 };
  const none = { ...event(5, 'n', 'content_revised'), attemptId: null };
  const s = summarizeAttempts([a1, a1done, a1dec, a2, none]);
  expect(s.map(a => [a.attemptId, a.epochs, a.lastKind, a.decision])).toEqual([
    ['pilot-r1', [1], 'decision_recorded', 'rejected'],
    ['pilot-r2', [2], 'attempt_started', null],
  ]);
});
