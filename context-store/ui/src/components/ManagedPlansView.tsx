// ManagedPlansView — read-only browser for plan-contract managed plans (plan 021)
//
// Left: managed-plan discovery (uuid cursor; Refresh resets it). Right: the selected plan as a
// tree, a dependency map and its history, plus the selected node's detail and history.
// GET only. Each data slot (list, plan, plan events, node events) has one live request; switching
// plan or node aborts the old ones and late responses are dropped before parsing (requestSlot).
// An unsupported contract version, an error-bearing plan or any failed/unsafe response clears
// the old view and shows an explicit error; no tree or map is derived from it.
//
// Depends on: planContract/api.ts, planContract/requestSlot.ts, managed/*
// Used by: App.tsx

import { useCallback, useEffect, useState } from 'react';
import { ContractApiError, getNodeEvents, getPlan, getPlanEvents, listPlans, type CheckedEventPage, type RequestGuard } from '../planContract/api';
import { isStale, useRequestSlot, type RequestSlot } from '../planContract/requestSlot';
import type { PlanListItem, PlanView } from '../planContract/types';
import DependencyMap from './managed/DependencyMap';
import EventsTimeline from './managed/EventsTimeline';
import { EMPTY_EVENTS, type EventsState } from './managed/eventsState';
import NodeDetail from './managed/NodeDetail';
import PlanTree from './managed/PlanTree';

type ErrorInfo = { code: string; message: string };
const toError = (e: unknown): ErrorInfo =>
  e instanceof ContractApiError ? { code: e.code, message: e.message } : { code: 'request_failed', message: (e as Error)?.message ?? String(e) };

interface ListState { status: 'loading' | 'ok' | 'error'; plans: PlanListItem[]; next: string | null; error: ErrorInfo | null }

type PlanState =
  | { status: 'none' }
  | { status: 'loading'; item: PlanListItem }
  | { status: 'unsupported'; item: PlanListItem }
  | { status: 'error'; item: PlanListItem; error: ErrorInfo; details: ErrorInfo[] }
  | { status: 'ok'; item: PlanListItem; plan: PlanView };

type Tab = 'tree' | 'map' | 'history';

/** Load one events page into a slot; appends only while the request is still the slot's latest. */
async function loadEvents(
  slot: RequestSlot, fetchPage: (cursor: string | null, g: RequestGuard) => Promise<CheckedEventPage>,
  cursor: string | null, set: (f: (s: EventsState) => EventsState) => void,
) {
  const g = slot.begin();
  // Pending is visible at once; existing rows stay while the next page loads (Load more is hidden).
  set(s => ({ ...s, status: 'loading', error: null }));
  try {
    const { page, nextCursorText } = await fetchPage(cursor, g);
    if (!g.isCurrent()) return;
    // Re-check inside the updater too: only the slot's latest request may change state.
    set(s => (!g.isCurrent() ? s : {
      status: 'ok', items: cursor === null ? page.events : [...s.items, ...page.events], nextCursorText,
      historyStartsAtSeq: page.historyStartsAtSeq, historyBackfilled: page.historyBackfilled, error: null,
    }));
  } catch (e) {
    if (isStale(e, g)) return;
    set(s => (!g.isCurrent() ? s : { ...EMPTY_EVENTS, status: 'error', error: toError(e) }));   // errors clear old rows
  }
}

export default function ManagedPlansView() {
  const listSlot = useRequestSlot();
  const planSlot = useRequestSlot();
  const planEventsSlot = useRequestSlot();
  const nodeEventsSlot = useRequestSlot();

  const [list, setList] = useState<ListState>({ status: 'loading', plans: [], next: null, error: null });
  const [plan, setPlan] = useState<PlanState>({ status: 'none' });
  const [nodeId, setNodeId] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('tree');
  const [planEvents, setPlanEvents] = useState<EventsState>(EMPTY_EVENTS);
  const [nodeEvents, setNodeEvents] = useState<EventsState>(EMPTY_EVENTS);

  const loadList = useCallback(async (cursor: string | null) => {
    const g = listSlot.begin();
    try {
      const page = await listPlans(cursor, g);
      if (!g.isCurrent()) return;
      setList(s => (!g.isCurrent() ? s : { status: 'ok', plans: cursor === null ? page.plans : [...s.plans, ...page.plans], next: page.nextAfterRootId, error: null }));
    } catch (e) {
      if (isStale(e, g)) return;
      setList(s => (!g.isCurrent() ? s : { status: 'error', plans: [], next: null, error: toError(e) }));   // never an empty "success"
    }
  }, [listSlot]);

  // Initial load from a timer callback (state is only ever set asynchronously, never in the effect body).
  useEffect(() => {
    const t = setTimeout(() => void loadList(null), 0);
    return () => clearTimeout(t);
  }, [loadList]);

  /** Refresh starts a new chain everywhere: every slot is cancelled and the selection cleared. */
  const refresh = () => {
    planSlot.cancel();
    planEventsSlot.cancel();
    nodeEventsSlot.cancel();
    setPlan({ status: 'none' });
    setNodeId(null);
    setPlanEvents(EMPTY_EVENTS);
    setNodeEvents(EMPTY_EVENTS);
    setList({ status: 'loading', plans: [], next: null, error: null });
    void loadList(null);
  };

  const selectPlan = (item: PlanListItem) => {
    planSlot.cancel();
    planEventsSlot.cancel();
    nodeEventsSlot.cancel();
    setNodeId(null);
    setNodeEvents(EMPTY_EVENTS);
    setPlanEvents(EMPTY_EVENTS);
    if (!item.supported) {
      setPlan({ status: 'unsupported', item });   // metadata only: no plan GET
      return;
    }
    setPlan({ status: 'loading', item });
    setPlanEvents({ ...EMPTY_EVENTS, status: 'loading' });
    const g = planSlot.begin();
    void (async () => {
      try {
        const view = await getPlan(item.rootId, g);
        if (!g.isCurrent()) return;
        if (view.rootId !== item.rootId) {
          setPlan(previous => g.isCurrent() ? { status: 'error', item, error: { code: 'unexpected_shape', message: 'The response is for a different plan.' }, details: [] } : previous);
          return;
        }
        if (view.readiness.errors.length > 0) {
          setPlan(previous => g.isCurrent() ? { status: 'error', item, error: { code: 'invalid_graph', message: 'The stored plan is invalid; nothing is derived from it.' },
            details: view.readiness.errors.map(e => ({ code: e.code, message: e.message })) } : previous);
          return;
        }
        setPlan(previous => g.isCurrent() ? { status: 'ok', item, plan: view } : previous);
      } catch (e) {
        if (isStale(e, g)) return;
        setPlan(previous => g.isCurrent() ? { status: 'error', item, error: toError(e), details: [] } : previous);
      }
    })();
    void loadEvents(planEventsSlot, (c, eg) => getPlanEvents(item.rootId, c, eg), null, setPlanEvents);
  };

  const selectNode = (id: string) => {
    if (plan.status !== 'ok') return;
    setNodeId(id);
    setNodeEvents({ ...EMPTY_EVENTS, status: 'loading' });
    void loadEvents(nodeEventsSlot, (c, g) => getNodeEvents(id, c, g), null, setNodeEvents);
  };

  const nameOf = (id: string) => (plan.status === 'ok' ? plan.plan.nodes.find(n => n.id === id)?.name : null) ?? id;

  return (
    <div data-testid="managed-plans" className="flex-1 flex overflow-hidden text-slate-200">
      <aside className="w-72 border-r border-slate-700 bg-slate-800/50 flex flex-col">
        <div className="flex items-center justify-between px-3 py-2 border-b border-slate-700">
          <span className="text-sm font-semibold">Managed plans</span>
          <button data-testid="managed-refresh" onClick={refresh} className="text-xs text-blue-400 hover:text-blue-300">Refresh</button>
        </div>
        <div className="flex-1 overflow-y-auto">
          {list.status === 'error' && list.error && (
            <div data-testid="managed-list-error" data-code={list.error.code} className="p-3 text-xs text-red-400">
              {list.error.code === 'not_found'
                ? 'The plan contract is not enabled on this API (HEKATE_PLAN_CONTRACT=1 is required).'
                : `Plans could not be listed (${list.error.code}): ${list.error.message}`}
            </div>
          )}
          {list.status === 'ok' && list.plans.length === 0 && (
            <div data-testid="managed-list-empty" className="p-3 text-xs text-slate-500">No managed plans.</div>
          )}
          <ul data-testid="managed-plan-list">
            {list.plans.map(p => (
              <li key={p.rootId}>
                <button
                  data-testid={`plan-item-${p.rootId}`} data-supported={String(p.supported)}
                  onClick={() => selectPlan(p)}
                  className={`w-full text-left px-3 py-2 border-b border-slate-800 hover:bg-slate-700/50 ${
                    plan.status !== 'none' && plan.item.rootId === p.rootId ? 'bg-slate-700' : ''}`}
                >
                  <div className="text-sm truncate">{p.name ?? p.rootId}</div>
                  <div className="text-[10px] text-slate-500">
                    {p.supported ? p.contractVersion : `${p.contractVersion} (unsupported)`} · gate {p.defaultGate} · {p.eventSeq} events
                  </div>
                </button>
              </li>
            ))}
          </ul>
          {list.status === 'loading' && <div className="p-3 text-xs text-slate-500">Loading…</div>}
          {list.status === 'ok' && list.next !== null && (
            <button data-testid="managed-list-more" onClick={() => { setList(s => ({ ...s, status: 'loading' })); void loadList(list.next); }}
              className="w-full p-2 text-xs text-blue-400 hover:text-blue-300">Load more</button>
          )}
        </div>
      </aside>

      <main className="flex-1 overflow-auto p-4">
        {plan.status === 'none' && <div className="text-sm text-slate-500">Select a managed plan. This view is read-only.</div>}
        {plan.status === 'loading' && <div data-testid="managed-plan-loading" className="text-sm text-slate-500">Loading plan…</div>}
        {plan.status === 'unsupported' && (
          <div data-testid="managed-plan-unsupported" className="text-sm text-amber-400">
            “{plan.item.name ?? plan.item.rootId}” uses contract version {plan.item.contractVersion}, which this browser does not support. It is not rendered.
          </div>
        )}
        {plan.status === 'error' && (
          <div data-testid="managed-plan-error" data-code={plan.error.code} className="text-sm text-red-400 space-y-1">
            <div>Plan “{plan.item.name ?? plan.item.rootId}” cannot be shown ({plan.error.code}): {plan.error.message}</div>
            {plan.details.map((d, i) => <div key={i} data-testid="managed-plan-error-detail" className="text-xs">{d.code}: {d.message}</div>)}
          </div>
        )}
        {plan.status === 'ok' && (
          <div data-testid="managed-plan" data-root-id={plan.plan.rootId} className="flex gap-4">
            <div className="flex-1 min-w-0">
              <div className="flex gap-2 mb-3">
                {(['tree', 'map', 'history'] as const).map(t => (
                  <button key={t} data-testid={`managed-tab-${t}`} onClick={() => setTab(t)}
                    className={`px-3 py-1 text-xs rounded ${tab === t ? 'bg-slate-600' : 'bg-slate-800 text-slate-400'}`}>{t}</button>
                ))}
              </div>
              {tab === 'tree' && <PlanTree plan={plan.plan} selectedId={nodeId} onSelect={selectNode} />}
              {tab === 'map' && (
                <div className="overflow-auto">
                  <div data-testid="map-legend" className="text-[11px] text-slate-500 mb-2">
                    Edges: green = accepted gate, blue = completed gate, red = currently blocking, dashed = plan default gate.
                    Dashed boxes are containers (status derived by the API from their children); leaves show work · effective acceptance.
                  </div>
                  <DependencyMap plan={plan.plan} selectedId={nodeId} onSelect={selectNode} />
                </div>
              )}
              {tab === 'history' && (
                <EventsTimeline testId="plan-events" state={planEvents} nameOf={nameOf}
                  onLoadMore={() => void loadEvents(planEventsSlot, (c, g) => getPlanEvents(plan.plan.rootId, c, g), planEvents.nextCursorText, setPlanEvents)} />
              )}
            </div>
            {nodeId !== null && (
              <div className="w-96 flex-shrink-0 border-l border-slate-700 pl-4">
                <NodeDetail plan={plan.plan} nodeId={nodeId} history={
                  <EventsTimeline testId="node-events" state={nodeEvents} nameOf={nameOf}
                    onLoadMore={() => void loadEvents(nodeEventsSlot, (c, g) => getNodeEvents(nodeId, c, g), nodeEvents.nextCursorText, setNodeEvents)} />
                } />
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
