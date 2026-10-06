// EventsTimeline — read-only attempt/review history page (plan 021).
//
// historyStartsAtSeq / historyBackfilled are shown verbatim: earlier work is unknown, never
// synthesised. "Load more" passes the cursor's checked source text back unchanged.
//
// Depends on: managed/eventsState.ts
// Used by: ManagedPlansView

import type { EventsState } from './eventsState';

interface Props {
  testId: string;
  state: EventsState;
  nameOf: (id: string) => string;
  onLoadMore: () => void;
}

export default function EventsTimeline({ testId, state, nameOf, onLoadMore }: Props) {
  if (state.status === 'error' && state.error)
    return (
      <div data-testid={`${testId}-error`} data-code={state.error.code} className="text-xs text-red-400">
        History could not be loaded ({state.error.code}): {state.error.message}
      </div>
    );
  return (
    <div data-testid={testId} className="text-xs">
      <div className="text-slate-500 mb-1">
        {state.historyStartsAtSeq === null ? 'No recorded history.' : `Recorded history starts at seq ${state.historyStartsAtSeq}.`}
        {!state.historyBackfilled && ' Earlier work (if any) is not recorded and was not backfilled.'}
      </div>
      <table className="w-full text-slate-300">
        <tbody>
          {state.items.map(e => (
            <tr key={e.seq} data-testid="event-row" data-seq={e.seq} className="border-t border-slate-800 align-top">
              <td className="pr-2 text-slate-500">{e.seq}</td>
              <td className="pr-2">{e.kind}</td>
              <td className="pr-2">{nameOf(e.nodeId)}</td>
              <td className="pr-2">{e.workFrom} → {e.workTo}</td>
              <td className="pr-2">{e.attemptId ? `${e.attemptId}/${e.attemptEpoch}` : ''}</td>
              <td className="pr-2">{e.decision ?? ''}{e.claimKey ? ` claim ${e.claimKey}` : ''}</td>
              <td className="pr-2 text-slate-400">{e.actor}</td>
              <td className="text-slate-500">{e.recordedAt}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {state.status === 'loading' && <div className="text-slate-500 mt-1">Loading…</div>}
      {state.status === 'ok' && state.nextCursorText !== null && (
        <button data-testid={`${testId}-more`} onClick={onLoadMore}
          className="mt-1 text-blue-400 hover:text-blue-300">Load more</button>
      )}
    </div>
  );
}
