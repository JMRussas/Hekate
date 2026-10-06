// Fixtures and API mocks for the managed-plan browser (plan 021).
//
// Response bodies follow the exact field sets of context-store/Api/PlanContractEndpoints.cs
// (list, View/ReadinessView, Events). Bodies are raw TEXT so tests can inject unsafe numbers.
import type { Page, Request } from '@playwright/test';
import { mockBaseAPIs } from './helpers';

export const V = 'plan-contract/v1';
export const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;

export const ROOT_A = id(1);
export const ROOT_B = id(2);
export const ROOT_BAD = id(3);
export const ROOT_FUTURE = id(4);
export const PROJECT = id(99);

// Plan A nodes
export const PHASE = id(10);
export const L1 = id(11);
export const NA = id(12);
export const NB = id(13);
export const NC = id(14);
export const X = id(15);
export const PHASE_DONE = id(16);
export const ND = id(17);

export function listItem(rootId: string, name: string, extra: Record<string, unknown> = {}) {
  return {
    rootId, projectId: PROJECT, name, defaultGate: 'accepted', contractVersion: V, supported: true,
    createdAt: '2026-10-06T10:00:00Z', createdBy: 'tester', eventSeq: 3, ...extra,
  };
}

export const listBody = (plans: unknown[], next: string | null = null) =>
  JSON.stringify({ contractVersion: V, plans, nextAfterRootId: next });

function node(nid: string, name: string, extra: Record<string, unknown> = {}) {
  return {
    id: nid, parentId: ROOT_A, nodeType: 'task', name, value: null, contentAttributes: {}, siblingOrder: 0,
    contentRevision: 1, stateRevision: 0, work: 'todo', attemptId: null, attemptEpoch: 0, artifactRef: null,
    executorRef: null, attemptContentRevision: null, attemptPrereqDigest: null, acceptance: null, effectiveAcceptance: 'none',
    ...extra,
  };
}

const accepted = (epoch: number) => ({
  decision: 'accepted', contentRevision: 1, artifactRef: 'sha-1', attemptId: 'a1', attemptEpoch: epoch, decidedBy: 'reviewer', evidenceRef: 'ev-1',
});

/**
 * Plan A: root -> phase P (container) -> L1; leaves A (done, accepted), B (A -> B, ready),
 * C (done, stale acceptance), X (todo). X -> L1 AND X -> P are two distinct declared edges, so
 * L1 is blocked twice: through itself and through its ancestor P.
 */
export function planA(overrides: { stateRevisionOfA?: string } = {}) {
  const nodes = [
    node(ROOT_A, 'Plan A', { parentId: null, nodeType: 'plan' }),
    node(PHASE, 'Phase P', { nodeType: 'plan_phase', siblingOrder: 1 }),
    node(L1, 'Leaf L1', { parentId: PHASE }),
    node(NA, 'Leaf A', { siblingOrder: 2, work: 'done', stateRevision: 3, attemptId: 'a1', attemptEpoch: 1, artifactRef: 'sha-1',
      attemptContentRevision: 1, attemptPrereqDigest: 'd'.repeat(64), acceptance: accepted(1), effectiveAcceptance: 'accepted',
      value: 'spec a', contentAttributes: { acceptance_criteria: 'green' } }),
    node(NB, 'Leaf B', { siblingOrder: 3 }),
    node(NC, 'Leaf C', { siblingOrder: 4, work: 'done', attemptId: 'c1', attemptEpoch: 1, acceptance: accepted(1), effectiveAcceptance: 'stale',
      attemptContentRevision: 1, attemptPrereqDigest: 'e'.repeat(64) }),
    node(PHASE_DONE, 'Phase Done', { nodeType: 'plan_phase', siblingOrder: 6 }),
    node(ND, 'Leaf D', { parentId: PHASE_DONE, work: 'done', attemptId: 'd1', attemptEpoch: 1, acceptance: accepted(1), effectiveAcceptance: 'accepted' }),
    node(X, 'Leaf X', { siblingOrder: 5 }),
  ];
  const deps = [
    { predecessorId: NA, successorId: NB, gate: null },
    { predecessorId: X, successorId: L1, gate: 'completed' },
    { predecessorId: X, successorId: PHASE, gate: null },
  ];
  const leaf = (nid: string, name: string, work: string, ready: boolean, blockers: unknown[] = []) => ({
    nodeId: nid, name, work, ready, gatesHold: blockers.length === 0, upstreamChanged: false, attemptId: null, attemptEpoch: 0, blockers,
  });
  const readiness = {
    contractVersion: V, rootId: ROOT_A, errors: [],
    leaves: [
      leaf(L1, 'Leaf L1', 'todo', false, [
        { ownerId: L1, predecessorId: X, gate: 'completed', reason: 'predecessor_not_completed' },
        { ownerId: PHASE, predecessorId: X, gate: 'accepted', reason: 'predecessor_not_completed' },
      ]),
      leaf(NA, 'Leaf A', 'done', false),
      leaf(NB, 'Leaf B', 'todo', true),
      leaf(NC, 'Leaf C', 'done', false),
      leaf(X, 'Leaf X', 'todo', true),
      leaf(ND, 'Leaf D', 'done', false),
    ],
    containers: [
      { nodeId: ROOT_A, name: 'Plan A', completion: 'incomplete', acceptance: 'pending', gatesHold: false },
      { nodeId: PHASE, name: 'Phase P', completion: 'incomplete', acceptance: 'pending', gatesHold: false },
      { nodeId: PHASE_DONE, name: 'Phase Done', completion: 'complete', acceptance: 'accepted', gatesHold: true },
    ],
  };
  let body = JSON.stringify({ contractVersion: V, outcome: null, rootId: ROOT_A, projectId: PROJECT, defaultGate: 'accepted', nodes, dependencies: deps, readiness });
  if (overrides.stateRevisionOfA !== undefined) body = body.replace('"stateRevision":3', `"stateRevision":${overrides.stateRevisionOfA}`);
  return body;
}

/** A minimal valid plan with one leaf (used as "plan B"). */
export function planB() {
  const leafId = id(21);
  return JSON.stringify({
    contractVersion: V, outcome: null, rootId: ROOT_B, projectId: PROJECT, defaultGate: 'accepted',
    nodes: [node(ROOT_B, 'Plan B', { parentId: null, nodeType: 'plan' }), node(leafId, 'Only leaf B', { parentId: ROOT_B })],
    dependencies: [],
    readiness: {
      contractVersion: V, rootId: ROOT_B, errors: [],
      leaves: [{ nodeId: leafId, name: 'Only leaf B', work: 'todo', ready: true, gatesHold: true, upstreamChanged: false, attemptId: null, attemptEpoch: 0, blockers: [] }],
      containers: [{ nodeId: ROOT_B, name: 'Plan B', completion: 'incomplete', acceptance: 'pending', gatesHold: true }],
    },
  });
}

/** A plan whose stored graph is invalid: the API returns readiness.errors and no leaves. */
export function planBad() {
  return JSON.stringify({
    contractVersion: V, outcome: null, rootId: ROOT_BAD, projectId: PROJECT, defaultGate: 'accepted',
    nodes: [node(ROOT_BAD, 'Bad plan', { parentId: null, nodeType: 'plan' })], dependencies: [],
    readiness: { contractVersion: V, rootId: ROOT_BAD, errors: [{ code: 'invalid_state', message: 'Attempt pins must be both set', nodeId: id(31), relatedId: null }], leaves: [], containers: [] },
  });
}

/** A plan with many leaves to exercise the map's table fallback. */
export function planWide(count: number) {
  const nodes = [node(ROOT_A, 'Wide', { parentId: null, nodeType: 'plan' })];
  const leaves = [];
  for (let i = 0; i < count; i++) {
    const nid = id(1000 + i);
    nodes.push(node(nid, `W${i}`));
    leaves.push({ nodeId: nid, name: `W${i}`, work: 'todo', ready: i === 0, gatesHold: true, upstreamChanged: false, attemptId: null, attemptEpoch: 0, blockers: [] });
  }
  const deps = [{ predecessorId: id(1000), successorId: id(1001), gate: null }];
  return JSON.stringify({
    contractVersion: V, outcome: null, rootId: ROOT_A, projectId: PROJECT, defaultGate: 'accepted', nodes, dependencies: deps,
    readiness: { contractVersion: V, rootId: ROOT_A, errors: [], leaves, containers: [{ nodeId: ROOT_A, name: 'Wide', completion: 'incomplete', acceptance: 'pending', gatesHold: true }] },
  });
}

export function event(seq: number, nodeId: string, kind = 'attempt_started') {
  return {
    seq, nodeId, nodeStateRevision: seq, kind, workFrom: 'todo', workTo: 'in_progress', contentRevision: 1, attemptId: 'a1', attemptEpoch: 1,
    executorRef: null, artifactRef: null, decision: null, reviewedContentRevision: null, evidenceRef: null, contentDigest: null,
    actor: 'worker', operationKey: `op-${seq}`, recordedAt: '2026-10-06T10:00:00Z', attemptContentRevision: 1, attemptPrereqDigest: 'd', claimKey: null,
  };
}

export const eventsBody = (events: unknown[], next: string | null, starts: number | null = 1) =>
  `{"contractVersion":"${V}","events":${JSON.stringify(events)},"nextAfterSeq":${next ?? 'null'},"historyStartsAtSeq":${starts ?? 'null'},"historyBackfilled":false}`;

export type Reply = { status?: number; body: string } | (() => Promise<{ status?: number; body: string }>);

/**
 * Installs the base mocks plus a plan-contract router. `routes` maps "path?query" prefixes (after
 * /api/plan-contract/v1) to replies; the longest matching prefix wins. Every API request is
 * recorded; non-GET API requests are collected separately for the read-only guard.
 */
export async function mockContract(page: Page, routes: Record<string, Reply>) {
  const requests: { method: string; url: string }[] = [];
  const nonGet: string[] = [];
  page.on('request', (r: Request) => {
    const u = new URL(r.url());
    if (!u.pathname.startsWith('/api/')) return;   // UI assets / Vite dev server requests are excluded
    requests.push({ method: r.method(), url: u.pathname + u.search });
    if (r.method() !== 'GET') nonGet.push(`${r.method()} ${u.pathname}`);
  });
  await mockBaseAPIs(page);
  await page.route('**/api/plan-contract/v1/**', async route => {
    const u = new URL(route.request().url());
    const key = u.pathname.replace('/api/plan-contract/v1', '') + u.search;
    const match = Object.keys(routes).filter(k => key.startsWith(k)).sort((a, b) => b.length - a.length)[0];
    if (match === undefined) {
      await route.fulfill({ status: 599, contentType: 'application/json', body: '{"code":"unmocked","message":"unmocked"}' });
      return;
    }
    const spec = routes[match];
    const reply = typeof spec === 'function' ? await spec() : spec;
    try {
      await route.fulfill({ status: reply.status ?? 200, contentType: 'application/json', body: reply.body });
    } catch {
      // The browser aborted this request (superseded); nothing to deliver.
    }
  });
  return { requests, nonGet };
}

/** A reply that is held until release() is called. */
export function held(reply: { status?: number; body: string }) {
  let release!: () => void;
  const gate = new Promise<void>(r => { release = r; });
  return { reply: async () => { await gate; return reply; }, release };
}

export async function openPlans(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: 'Plans', exact: true }).click();
}
