// Attempt-trace slot state shared by ManagedPlansView and AttemptTrace (trace contract v0).
//
// `records` accumulate across pages. `lastSeq` is the cursor for the next request: it is the
// last record held, so a trace whose attempt is still in progress can be asked for more even
// when the previous page reported no further records (nextAfterSeq null). The header fields
// always come from the latest page; the prompt only from the first.
//
// Depends on: planContract/types.ts
// Used by: ManagedPlansView, managed/AttemptTrace.tsx, managed/AttemptsPanel.tsx

import type { AttemptTracePageView, PlanEventView, TraceRecordView } from '../../planContract/types';

export interface AttemptSummary {
  attemptId: string;
  epochs: number[];
  firstSeq: number;
  firstAt: string;
  lastAt: string;
  lastKind: string;
  decision: string | null;
}

/** Group event rows by attemptId, in first-seen order; rows without an attempt are ignored. */
export function summarizeAttempts(events: readonly PlanEventView[]): AttemptSummary[] {
  const byId = new Map<string, AttemptSummary>();
  for (const e of events) {
    if (e.attemptId === null) continue;
    let a = byId.get(e.attemptId);
    if (!a) {
      a = { attemptId: e.attemptId, epochs: [], firstSeq: e.seq, firstAt: e.recordedAt, lastAt: e.recordedAt, lastKind: e.kind, decision: null };
      byId.set(e.attemptId, a);
    }
    if (!a.epochs.includes(e.attemptEpoch)) a.epochs.push(e.attemptEpoch);
    a.lastAt = e.recordedAt;
    a.lastKind = e.kind;
    if (e.decision !== null) a.decision = e.decision;
  }
  return [...byId.values()].sort((x, y) => x.firstSeq - y.firstSeq);
}

export interface TraceState {
  status: 'idle' | 'loading' | 'ok' | 'error';
  attemptId: string | null;
  page: Omit<AttemptTracePageView, 'records' | 'prompt'> | null;
  prompt: AttemptTracePageView['prompt'];
  records: TraceRecordView[];
  lastSeq: number | null;
  error: { code: string; message: string } | null;
}

export const EMPTY_TRACE: TraceState = {
  status: 'idle', attemptId: null, page: null, prompt: null, records: [], lastSeq: null, error: null,
};

/** Fold one checked page into the state; afterSeq null means the page starts the trace. */
export function applyTracePage(s: TraceState, page: AttemptTracePageView, afterSeq: number | null): TraceState {
  const { records, prompt, ...header } = page;
  const all = afterSeq === null ? records : [...s.records, ...records];
  return {
    status: 'ok',
    attemptId: page.attemptId,
    page: header,
    prompt: afterSeq === null ? prompt : s.prompt,
    records: all,
    lastSeq: all.length > 0 ? all[all.length - 1].seq : afterSeq,
    error: null,
  };
}
