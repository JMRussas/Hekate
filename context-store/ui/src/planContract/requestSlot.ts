// Per-view request slots: stale-response isolation for the managed-plan browser (plan 021 §2.2a)
//
// Each slot (list, plan, plan events, node events) has ONE live request. begin() aborts the
// previous one and issues a new token; the returned guard's isCurrent() is checked by the client
// after the body arrives and before parsing, and again by the view before any state update. A
// response for an earlier selection can therefore never paint, or raise an error, into a later one.
//
// Depends on: planContract/api.ts (RequestGuard, StaleResponseError)
// Used by: components/ManagedPlansView.tsx

import { useEffect, useMemo, useRef } from 'react';
import { StaleResponseError, type RequestGuard } from './api';

export interface RequestSlot {
  begin(): RequestGuard;
  cancel(): void;
}

export function useRequestSlot(): RequestSlot {
  const state = useRef<{ ctrl: AbortController | null; token: number }>({ ctrl: null, token: 0 });
  useEffect(() => {
    const s = state.current;
    return () => {
      s.ctrl?.abort();
      s.token++;
    };
  }, []);
  return useMemo<RequestSlot>(() => ({
    begin() {
      const s = state.current;
      s.ctrl?.abort();
      const ctrl = new AbortController();
      const token = ++s.token;
      s.ctrl = ctrl;
      return { signal: ctrl.signal, isCurrent: () => s.token === token && !ctrl.signal.aborted };
    },
    cancel() {
      const s = state.current;
      s.ctrl?.abort();
      s.ctrl = null;
      s.token++;
    },
  }), []);
}

/** True for responses that must be dropped silently (superseded or aborted). */
export function isStale(e: unknown, guard: RequestGuard): boolean {
  return e instanceof StaleResponseError
    || (e instanceof DOMException && e.name === 'AbortError')
    || !guard.isCurrent();
}
