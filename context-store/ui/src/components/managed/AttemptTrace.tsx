// AttemptTrace — one attempt's retained worker output (trace contract v0, rev 2).
//
// Read-only and manual: "Reload trace" fetches it again from the start, "Load more" follows the
// page cursor, and "Check for new records" asks again after the last record held while the
// attempt is still in progress. The header states what is known about the trace (status and
// integrity); it never states acceptance, which stays in the node's Acceptance section. All
// worker text is rendered as plain text nodes, never as HTML or markdown.
//
// Depends on: managed/traceState.ts, managed/traceNormalize.ts, managed/labels.ts
// Used by: ManagedPlansView (inside AttemptsPanel)

import { useState } from 'react';
import { TRACE_ERRORS, traceStatusLabel } from './labels';
import { normalizeTrace, type TraceItem } from './traceNormalize';
import type { TraceState } from './traceState';

const ITEM_LIMIT = 20_000;

const ROLE_STYLE: Record<string, string> = {
  assistant: 'text-slate-100',
  tool_call: 'text-sky-300',
  tool_result: 'text-slate-300',
  result: 'text-emerald-300',
  system: 'text-slate-400',
  stderr: 'text-amber-300',
  note: 'text-slate-500 italic',
  raw: 'text-slate-400',
};

interface Props {
  state: TraceState;
  onReload: () => void;
  onMore: () => void;
}

export default function AttemptTrace({ state, onReload, onMore }: Props) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  if (state.attemptId === null) return null;
  const reload = (
    <button data-testid="trace-reload" onClick={onReload} className="text-blue-400 hover:text-blue-300">Reload trace</button>
  );
  if (state.status === 'error' && state.error)
    return (
      <div data-testid="attempt-trace-error" data-code={state.error.code} className="mt-2 text-red-400 space-y-1">
        <div>{TRACE_ERRORS[state.error.code] ??`The trace could not be loaded (${state.error.code}): ${state.error.message}`}</div>
        {reload}
      </div>
    );
  const page = state.page;
  if (page === null) return <div data-testid="attempt-trace-loading" className="mt-2 text-slate-500">Loading trace…</div>;

  const items = normalizeTrace(state.records, page.executionKind);
  const toggle = (k: string) => setExpanded(prev => {
    const next = new Set(prev);
    if (next.has(k)) next.delete(k); else next.add(k);
    return next;
  });
  const canMore = state.status === 'ok' && page.nextAfterSeq !== null;
  const canCheck = state.status === 'ok' && page.nextAfterSeq === null && page.status === 'running';

  return (
    <div data-testid="attempt-trace" data-attempt-id={page.attemptId} data-status={page.status}
      data-integrity={page.integrity} className="mt-2 border-t border-slate-700 pt-2 space-y-2">
      <div data-testid="trace-header" className="space-y-0.5">
        <div data-testid="trace-status" className="text-slate-200">{traceStatusLabel(page.status, page.reason, page.integrity)}</div>
        <div className="text-[10px] text-slate-500">
          {page.executionKind ?? 'unknown kind'} · epoch {page.attemptEpoch}{page.claimKey ? ` · claim ${page.claimKey}` : ''}
          {page.exit && ` · exit ${page.exit.code ?? '—'}${page.exit.killReason ? ` (${page.exit.killReason})` : ''}`}
        </div>
        <div className="text-[10px] text-slate-500">The worker’s own result is not the review decision; see Acceptance.</div>
      </div>

      {state.prompt && (
        <details data-testid="trace-prompt">
          <summary className="cursor-pointer text-slate-400">Prompt ({state.prompt.bytes} bytes)</summary>
          <TextBlock text={state.prompt.text} id="prompt" expanded={expanded} onToggle={toggle} />
        </details>
      )}

      <div data-testid="trace-items" className="space-y-1">
        {items.map(it => <Item key={it.key} item={it} expanded={expanded} onToggle={toggle} />)}
      </div>

      {page.capped && <div data-testid="trace-capped" className="text-slate-500">The supervisor capped this trace; later output was not retained.</div>}
      {state.status === 'loading' && <div className="text-slate-500">Loading…</div>}
      <div className="flex gap-3">
        {reload}
        {canMore && <button data-testid="trace-more" onClick={onMore} className="text-blue-400 hover:text-blue-300">Load more</button>}
        {canCheck && <button data-testid="trace-check" onClick={onMore} className="text-blue-400 hover:text-blue-300">Check for new records</button>}
      </div>
    </div>
  );
}

function Item({ item, expanded, onToggle }: { item: TraceItem; expanded: ReadonlySet<string>; onToggle: (k: string) => void }) {
  const label = item.role === 'tool_call' || item.role === 'tool_result'
    ? `${item.role === 'tool_call' ? 'tool call' : 'tool result'}${item.toolName ? ` · ${item.toolName}` : ''}`
    : item.role;
  return (
    <div data-testid="trace-item" data-role={item.role} data-seq={item.seq} data-tool-id={item.toolId}
      data-error={item.isError ? 'true' : undefined} className={ROLE_STYLE[item.role] ?? ''}>
      <div className={`text-[10px] uppercase ${item.isError ? 'text-red-400' : 'text-slate-500'}`}>
        {label}{item.isError ? ' · error' : ''}{item.cut ? ' · line truncated by supervisor' : ''}
      </div>
      <TextBlock text={item.text} id={item.key} expanded={expanded} onToggle={onToggle} />
    </div>
  );
}

function TextBlock({ text, id, expanded, onToggle }: { text: string; id: string; expanded: ReadonlySet<string>; onToggle: (k: string) => void }) {
  const long = text.length > ITEM_LIMIT;
  const open = expanded.has(id);
  return (
    <div>
      <pre className="whitespace-pre-wrap break-words font-mono text-[11px]">{long && !open ? text.slice(0, ITEM_LIMIT) : text}</pre>
      {long && (
        <button data-testid="trace-expand" onClick={() => onToggle(id)} className="text-blue-400 hover:text-blue-300">
          {open ? 'Show less' : `Show all (${text.length} characters)`}
        </button>
      )}
    </div>
  );
}
