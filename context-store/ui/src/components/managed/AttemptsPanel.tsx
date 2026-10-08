// AttemptsPanel — a node's attempts, derived from its already-loaded events (trace contract v0).
//
// Attempts are grouped by attemptId from the node-event rows the view already holds; no other
// request is made. While more history pages exist the list may be partial, and it says so.
// Selecting an attempt shows its trace below the list.
//
// Depends on: planContract/types.ts, managed/traceState.ts
// Used by: ManagedPlansView (rendered inside NodeDetail's Attempts section)

import type { ReactNode } from 'react';
import type { PlanEventView } from '../../planContract/types';
import { summarizeAttempts } from './traceState';

interface Props {
  events: readonly PlanEventView[];
  partial: boolean;
  selectedAttemptId: string | null;
  onSelect: (attemptId: string) => void;
  trace: ReactNode;
}

export default function AttemptsPanel({ events, partial, selectedAttemptId, onSelect, trace }: Props) {
  const attempts = summarizeAttempts(events);
  return (
    <div data-testid="node-attempts" className="text-xs space-y-1">
      {attempts.length === 0
        ? <div className="text-slate-500">No attempts in the loaded history.</div>
        : attempts.map(a => (
          <button key={a.attemptId} data-testid="attempt-row" data-attempt-id={a.attemptId}
            data-selected={String(a.attemptId === selectedAttemptId)} onClick={() => onSelect(a.attemptId)}
            className={`w-full text-left px-2 py-1 rounded border border-slate-700 hover:bg-slate-700/50 ${
              a.attemptId === selectedAttemptId ? 'bg-slate-700' : ''}`}>
            <div className="text-slate-200 font-mono break-all">{a.attemptId}</div>
            <div className="text-[10px] text-slate-500">
              epoch {a.epochs.join(', ')} · last {a.lastKind}{a.decision ? ` · decision ${a.decision}` : ''} · {a.firstAt} → {a.lastAt}
            </div>
          </button>
        ))}
      {partial && <div data-testid="attempts-partial" className="text-slate-500">More history exists; load it to see earlier attempts.</div>}
      {trace}
    </div>
  );
}
