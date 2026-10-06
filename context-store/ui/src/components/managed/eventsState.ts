// Event-history slot state shared by ManagedPlansView and EventsTimeline (plan 021).
//
// Depends on: planContract/types.ts
// Used by: ManagedPlansView, managed/EventsTimeline.tsx

import type { PlanEventView } from '../../planContract/types';

export interface EventsState {
  status: 'idle' | 'loading' | 'ok' | 'error';
  items: PlanEventView[];
  nextCursorText: string | null;
  historyStartsAtSeq: number | null;
  historyBackfilled: boolean;
  error: { code: string; message: string } | null;
}

export const EMPTY_EVENTS: EventsState = {
  status: 'idle', items: [], nextCursorText: null, historyStartsAtSeq: null, historyBackfilled: false, error: null,
};
