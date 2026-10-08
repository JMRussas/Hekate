// Attempt-trace normalizer (trace contract v0, rev 2) — raw records to display items.
//
// Pure: the same records and executionKind always give the same items. Each backend has its
// own mapping; anything unrecognised is shown raw, never dropped or guessed. The supervisor
// removes private reasoning before retention (a record then has redacted=true); its visible
// text and tool items are still shown, followed by an omission note. Reasoning that reaches
// the browser anyway is not displayed. Acceptance is never derived from a trace.
//
// Depends on: planContract/types.ts
// Used by: managed/AttemptTrace.tsx, e2e/managed-trace-normalize.spec.ts

import type { TraceRecordView } from '../../planContract/types';

export type TraceRole = 'system' | 'assistant' | 'tool_call' | 'tool_result' | 'result' | 'stderr' | 'note' | 'raw';

export interface TraceItem {
  /** Unique within one normalization: the record seq plus the item's index in that record. */
  key: string;
  seq: number;
  role: TraceRole;
  text: string;
  toolName?: string;
  toolId?: string;
  isError?: boolean;
  /** The supervisor cut this line at its per-line cap. */
  cut?: boolean;
}

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);
const s = (v: unknown): string | undefined => (typeof v === 'string' ? v : undefined);
const compact = (v: unknown): string => (typeof v === 'string' ? v : JSON.stringify(v) ?? '');

export const REASONING_OMITTED = 'Reasoning omitted.';

const NOTE_LABELS: Record<string, string> = {
  stdout_cap_reached: 'Output cap reached; later output was not retained.',
  stderr_cap_reached: 'Error-output cap reached; later error output was not retained.',
};

/** Supervisor notes have fixed texts; prefixes carry a value (killed:<reason>, exit:<code>, ...). */
function hekateNote(text: string): string {
  if (NOTE_LABELS[text]) return NOTE_LABELS[text];
  const [head, ...rest] = text.split(':');
  const value = rest.join(':');
  if (head === 'killed') return `Supervisor stopped the worker (${value}).`;
  if (head === 'exit') return `Worker exited with code ${value}.`;
  if (head === 'trace_incomplete') return `Trace incomplete (${value}).`;
  return `Supervisor: ${text}`;
}

interface Emit {
  (role: TraceRole, text: string, extra?: Partial<TraceItem>): void;
}

/** Claude Code `--output-format stream-json` lines. */
function claudeLine(line: Obj, emit: Emit, omitted: () => void) {
  const type = s(line.type);
  if (type === 'stream_event') return;   // partial deltas; the complete message follows
  if (type === 'system' && line.subtype === 'permission_denied') {
    // The CLI refused a tool use: name the tool and keep its own explanation.
    emit('system', `permission denied${s(line.tool_name) ? ` · ${line.tool_name}` : ''}${s(line.message) ? `: ${line.message}` : ''}`,
      { toolName: s(line.tool_name), toolId: s(line.tool_use_id), isError: true });
    return;
  }
  if (type === 'system') {
    const parts = [s(line.subtype) ?? 'system'];
    if (s(line.model)) parts.push(`model ${line.model}`);
    if (s(line.cwd)) parts.push(`cwd ${line.cwd}`);
    if (Array.isArray(line.tools)) parts.push(`${line.tools.length} tools`);
    emit('system', parts.join(' · '));
    return;
  }
  if (type === 'result') {
    const parts = [s(line.subtype) ?? 'result'];
    if (typeof line.num_turns === 'number') parts.push(`${line.num_turns} turns`);
    if (typeof line.total_cost_usd === 'number') parts.push(`$${line.total_cost_usd}`);
    if (typeof line.duration_ms === 'number') parts.push(`${line.duration_ms} ms`);
    emit('result', parts.join(' · '), { isError: line.is_error === true });
    return;
  }
  if ((type === 'assistant' || type === 'user') && isObj(line.message) && Array.isArray(line.message.content)) {
    for (const block of line.message.content) {
      if (!isObj(block)) continue;
      const bt = s(block.type);
      if (bt === 'text') emit(type === 'assistant' ? 'assistant' : 'raw', s(block.text) ?? '');
      else if (bt === 'tool_use') emit('tool_call', compact(block.input), { toolName: s(block.name), toolId: s(block.id) });
      else if (bt === 'tool_result') {
        const c = block.content;
        const text = Array.isArray(c) ? c.map(p => (isObj(p) && typeof p.text === 'string' ? p.text : compact(p))).join('\n') : compact(c);
        emit('tool_result', text, { toolId: s(block.tool_use_id), isError: block.is_error === true });
      } else if (bt === 'thinking' || bt === 'redacted_thinking') omitted();
      else emit('raw', compact(block));
    }
    return;
  }
  emit('raw', JSON.stringify(line));
}

/** Codex `exec --json` lines. item.started and item.completed share item.id. */
function codexLine(line: Obj, emit: Emit, omitted: () => void, started: Set<string>) {
  const type = s(line.type);
  if (type === 'thread.started') { emit('system', `thread started${s(line.thread_id) ? ` · ${line.thread_id}` : ''}`); return; }
  if (type === 'turn.started') { emit('system', 'turn started'); return; }
  if (type === 'turn.completed') { emit('result', `turn completed · ${compact(line.usage)}`); return; }
  if (type === 'turn.failed') { emit('result', `turn failed · ${compact(isObj(line.error) ? line.error.message ?? line.error : line.error)}`, { isError: true }); return; }
  if (type === 'error') { emit('system', s(line.message) ?? compact(line), { isError: true }); return; }
  if ((type === 'item.started' || type === 'item.updated' || type === 'item.completed') && isObj(line.item)) {
    const item = line.item;
    const id = s(item.id);
    const it = s(item.type);
    if (it === 'reasoning') { if (type === 'item.completed') omitted(); return; }
    if (it === 'agent_message') { if (type === 'item.completed') emit('assistant', s(item.text) ?? ''); return; }
    if (it === 'command_execution' || it === 'mcp_tool_call' || it === 'file_change' || it === 'web_search') {
      const name = it === 'mcp_tool_call' ? [s(item.server), s(item.tool)].filter(Boolean).join('/') || it : it;
      const call = it === 'command_execution' ? s(item.command) ?? '' : compact(item.arguments ?? item.changes ?? item.query ?? item);
      if (type === 'item.updated') return;
      if (type === 'item.started') {
        if (id) started.add(id);
        emit('tool_call', call, { toolName: name, toolId: id });
        return;
      }
      // completed: emit the call only when its start was never seen (no duplicate start).
      if (!id || !started.has(id)) emit('tool_call', call, { toolName: name, toolId: id });
      const exitCode = typeof item.exit_code === 'number' ? item.exit_code : null;
      const failed = s(item.status) === 'failed' || (exitCode !== null && exitCode !== 0);
      const output = it === 'command_execution'
        ? `${s(item.aggregated_output) ?? ''}${exitCode === null ? '' : `\n[exit ${exitCode}]`}`
        : compact(item.result ?? item.error ?? item.status ?? '');
      emit('tool_result', output, { toolName: name, toolId: id, isError: failed });
      return;
    }
    if (it === 'error') { emit('system', s(item.message) ?? compact(item), { isError: true }); return; }
    if (type === 'item.completed') emit('raw', JSON.stringify(line));
    return;
  }
  emit('raw', JSON.stringify(line));
}

export function normalizeTrace(records: readonly TraceRecordView[], executionKind: string | null): TraceItem[] {
  const items: TraceItem[] = [];
  const started = new Set<string>();
  const toolNames = new Map<string, string>();
  for (const r of records) {
    let n = 0;
    let noted = false;
    const emit: Emit = (role, text, extra = {}) => {
      const item: TraceItem = { key: `${r.seq}.${n++}`, seq: r.seq, role, text, ...extra };
      if (r.cut) item.cut = true;
      if (item.toolId && item.toolName) toolNames.set(item.toolId, item.toolName);
      if (role === 'tool_result' && item.toolId && !item.toolName) item.toolName = toolNames.get(item.toolId);
      items.push(item);
    };
    const omitted = () => {
      if (!noted) emit('note', REASONING_OMITTED);
      noted = true;
    };
    if (r.stream === 'stderr') emit('stderr', r.text);
    else if (r.stream === 'hekate') emit('note', hekateNote(r.text));
    else {
      let line: unknown;
      try { line = JSON.parse(r.text); } catch { line = undefined; }
      if (!isObj(line)) emit('raw', r.text);
      else if (executionKind === 'claude-cli' || executionKind === 'fake-cli') claudeLine(line, emit, omitted);
      else if (executionKind === 'codex-cli') codexLine(line, emit, omitted, started);
      else emit('raw', r.text);
    }
    if (r.redacted) omitted();   // after the record's visible items, once
  }
  return items;
}
