// Plan contract v1 read-only client (plan 021)
//
// GET only. Every response body is read as text, dropped if its request was superseded, then
// passed through parseContractJson (the integer-safety guard) and a full shape check; any
// failure is a typed ContractApiError and is never turned into an empty result.
//
// Depends on: planContract/json.ts, planContract/types.ts
// Used by: components/ManagedPlansView.tsx, planContract/requestSlot.ts

import { ContractJsonError, parseContractJson } from './json';
import {
  SUPPORTED_CONTRACT_VERSION,
  TRACE_INTEGRITY,
  TRACE_REASONS,
  TRACE_STATUSES,
  TRACE_STREAMS,
  type AttemptTracePageView,
  type EventPageView,
  type PlanListPage,
  type PlanView,
} from './types';

const BASE = '/api/plan-contract/v1';

export class ContractApiError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = 'ContractApiError';
    this.status = status;
    this.code = code;
  }
}

/** Thrown when a response belongs to a superseded request; views drop it silently. */
export class StaleResponseError extends Error {
  constructor() {
    super('Superseded response dropped before parsing.');
    this.name = 'StaleResponseError';
  }
}

/** A request's cancellation signal plus its acceptance check (is it still the slot's latest?). */
export interface RequestGuard {
  signal: AbortSignal;
  isCurrent: () => boolean;
}

async function getChecked<T>(path: string, req: RequestGuard, captureKeys: readonly string[] = []) {
  const { signal } = req;
  const res = await fetch(BASE + path, { method: 'GET', signal, headers: { Accept: 'application/json' } });
  const text = await res.text();
  // Acceptance check AFTER the body arrived but BEFORE anything is parsed, so a stale response
  // (even an unsafe or malformed one) can never raise a result or an error into the current view.
  if (signal.aborted || !req.isCurrent()) throw new StaleResponseError();
  if (!res.ok) {
    let code = res.status === 404 ? 'not_found' : 'http_error';
    let message = `HTTP ${res.status}`;
    try {
      const body = parseContractJson<{ code?: unknown; message?: unknown }>(text).value;
      if (body && typeof body.code === 'string') code = body.code;
      if (body && typeof body.message === 'string') message = body.message;
    } catch {
      // Non-JSON or unsafe error body: keep the generic code; never render the body.
    }
    throw new ContractApiError(res.status, code, message);
  }
  try {
    return parseContractJson<T>(text, captureKeys);
  } catch (e) {
    if (e instanceof ContractJsonError) throw new ContractApiError(res.status, e.code, e.message);
    throw e;
  }
}

// --- shape checks (fail closed on anything unexpected) ----------------------
//
// Every field the views consume is checked: type, enum membership, counter range, and
// cross-references (the plan root, readiness/dependency/blocker node ids, node-event node ids).
// Free text (names, values, attributes, refs) may be any string, including numeric strings.

function bad(what: string): never {
  throw new ContractApiError(200, 'unexpected_shape', `Unexpected response shape: ${what}`);
}
type Obj = Record<string, unknown>;
const obj = (v: unknown, what: string): Obj =>
  (typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Obj) : bad(what));
const str = (o: Obj, k: string, what: string) => { if (typeof o[k] !== 'string') bad(`${what}.${k}`); };
const optStr = (o: Obj, k: string, what: string) => { if (o[k] !== null && typeof o[k] !== 'string') bad(`${what}.${k}`); };
const bool = (o: Obj, k: string, what: string) => { if (typeof o[k] !== 'boolean') bad(`${what}.${k}`); };
const count = (o: Obj, k: string, what: string, min = 0) => {
  const v = o[k];
  if (typeof v !== 'number' || !Number.isSafeInteger(v) || v < min) bad(`${what}.${k}`);
};
const optCount = (o: Obj, k: string, what: string, min = 0) => { if (o[k] !== null) count(o, k, what, min); };
const oneOf = (o: Obj, k: string, what: string, values: readonly string[], nullable = false) => {
  const v = o[k];
  if (nullable && v === null) return;
  if (typeof v !== 'string' || !values.includes(v)) bad(`${what}.${k}`);
};
const arr = (o: Obj, k: string, what: string): unknown[] => (Array.isArray(o[k]) ? (o[k] as unknown[]) : bad(`${what}.${k}`));

const WORK = ['todo', 'in_progress', 'done', 'cancelled'];
const GATES = ['completed', 'accepted'];
const DECISIONS = ['accepted', 'rejected'];
const EFFECTIVE = ['none', 'accepted', 'rejected', 'stale'];
const COMPLETION = ['empty', 'incomplete', 'complete'];
const CONTAINER_ACCEPTANCE = ['empty', 'pending', 'accepted', 'rejected'];
const REASONS = ['predecessor_not_completed', 'predecessor_not_accepted', 'predecessor_acceptance_stale',
  'predecessor_rejected', 'predecessor_cancelled', 'predecessor_upstream_changed'];
const EVENT_KINDS = ['attempt_started', 'attempt_reopened', 'attempt_finished', 'attempt_released', 'attempt_cancelled',
  'work_restored', 'decision_recorded', 'content_revised'];

function checkVersion(v: Obj, what: string) {
  if (v.contractVersion !== SUPPORTED_CONTRACT_VERSION)
    throw new ContractApiError(200, 'unsupported_contract_version',
      `${what} uses '${String(v.contractVersion)}'; this browser supports '${SUPPORTED_CONTRACT_VERSION}'.`);
}

function checkListPage(v: unknown): PlanListPage {
  const o = obj(v, 'plan list');
  checkVersion(o, 'The plan list');
  optStr(o, 'nextAfterRootId', 'plan list');
  for (const raw of arr(o, 'plans', 'plan list')) {
    const p = obj(raw, 'plan list item');
    for (const k of ['rootId', 'projectId', 'contractVersion', 'createdAt', 'createdBy', 'defaultGate']) str(p, k, 'plan list item');
    optStr(p, 'name', 'plan list item');
    bool(p, 'supported', 'plan list item');
    count(p, 'eventSeq', 'plan list item');
    if (p.supported !== (p.contractVersion === SUPPORTED_CONTRACT_VERSION)) bad('plan list item.supported');
  }
  return o as unknown as PlanListPage;
}

function checkPlan(v: unknown, requestedRoot: string): PlanView {
  const o = obj(v, 'plan');
  checkVersion(o, 'The plan');
  if (o.rootId !== requestedRoot) throw new ContractApiError(200, 'unexpected_shape', 'The response is for a different plan.');
  str(o, 'projectId', 'plan');
  optStr(o, 'outcome', 'plan');
  oneOf(o, 'defaultGate', 'plan', GATES);

  const ids = new Set<string>();
  for (const raw of arr(o, 'nodes', 'plan')) {
    const n = obj(raw, 'node');
    str(n, 'id', 'node');
    str(n, 'nodeType', 'node');
    for (const k of ['parentId', 'name', 'value', 'attemptId', 'artifactRef', 'executorRef', 'attemptPrereqDigest']) optStr(n, k, 'node');
    const attrs = obj(n.contentAttributes, 'node.contentAttributes');
    if (Object.values(attrs).some(a => typeof a !== 'string')) bad('node.contentAttributes');
    count(n, 'siblingOrder', 'node', Number.MIN_SAFE_INTEGER);
    count(n, 'contentRevision', 'node', 1);
    count(n, 'stateRevision', 'node');
    count(n, 'attemptEpoch', 'node');
    optCount(n, 'attemptContentRevision', 'node', 1);
    oneOf(n, 'work', 'node', WORK);
    oneOf(n, 'effectiveAcceptance', 'node', EFFECTIVE);
    if (n.acceptance !== null) {
      const a = obj(n.acceptance, 'node.acceptance');
      oneOf(a, 'decision', 'acceptance', DECISIONS);
      count(a, 'contentRevision', 'acceptance', 1);
      count(a, 'attemptEpoch', 'acceptance');
      str(a, 'decidedBy', 'acceptance');
      for (const k of ['artifactRef', 'attemptId', 'evidenceRef']) optStr(a, k, 'acceptance');
    }
    if (ids.has(n.id as string)) bad('duplicate node id');
    ids.add(n.id as string);
  }
  if (!ids.has(requestedRoot)) bad('plan root node missing');
  const known = (id: unknown, what: string) => { if (typeof id !== 'string' || !ids.has(id)) bad(`${what} references an unknown node`); };

  for (const raw of arr(o, 'dependencies', 'plan')) {
    const d = obj(raw, 'dependency');
    known(d.predecessorId, 'dependency');
    known(d.successorId, 'dependency');
    oneOf(d, 'gate', 'dependency', GATES, true);
  }
  const r = obj(o.readiness, 'readiness');
  checkVersion(r, 'The readiness view');
  if (r.rootId !== requestedRoot) bad('readiness.rootId');
  for (const raw of arr(r, 'errors', 'readiness')) {
    const e = obj(raw, 'readiness error');
    str(e, 'code', 'readiness error');
    str(e, 'message', 'readiness error');
    optStr(e, 'nodeId', 'readiness error');
    optStr(e, 'relatedId', 'readiness error');
  }
  for (const raw of arr(r, 'leaves', 'readiness')) {
    const l = obj(raw, 'leaf');
    known(l.nodeId, 'leaf');
    optStr(l, 'name', 'leaf');
    oneOf(l, 'work', 'leaf', WORK);
    for (const k of ['ready', 'gatesHold', 'upstreamChanged']) bool(l, k, 'leaf');
    optStr(l, 'attemptId', 'leaf');
    count(l, 'attemptEpoch', 'leaf');
    for (const rawB of arr(l, 'blockers', 'leaf')) {
      const b = obj(rawB, 'blocker');
      known(b.ownerId, 'blocker');
      known(b.predecessorId, 'blocker');
      oneOf(b, 'gate', 'blocker', GATES);
      oneOf(b, 'reason', 'blocker', REASONS);
    }
  }
  for (const raw of arr(r, 'containers', 'readiness')) {
    const c = obj(raw, 'container');
    known(c.nodeId, 'container');
    optStr(c, 'name', 'container');
    oneOf(c, 'completion', 'container', COMPLETION);
    oneOf(c, 'acceptance', 'container', CONTAINER_ACCEPTANCE);
    bool(c, 'gatesHold', 'container');
  }
  return o as unknown as PlanView;
}

function checkEvents(v: unknown, requestedNode: string | null): EventPageView {
  const o = obj(v, 'event page');
  checkVersion(o, 'The event page');
  optCount(o, 'nextAfterSeq', 'event page');
  optCount(o, 'historyStartsAtSeq', 'event page', 1);
  bool(o, 'historyBackfilled', 'event page');
  let previous = 0;
  for (const raw of arr(o, 'events', 'event page')) {
    const e = obj(raw, 'event');
    count(e, 'seq', 'event', 1);
    if ((e.seq as number) <= previous) bad('event.seq order');
    previous = e.seq as number;
    str(e, 'nodeId', 'event');
    if (requestedNode !== null && e.nodeId !== requestedNode)
      throw new ContractApiError(200, 'unexpected_shape', 'The history contains an event for a different node.');
    oneOf(e, 'kind', 'event', EVENT_KINDS);
    oneOf(e, 'workFrom', 'event', WORK);
    oneOf(e, 'workTo', 'event', WORK);
    count(e, 'nodeStateRevision', 'event');
    count(e, 'contentRevision', 'event', 1);
    count(e, 'attemptEpoch', 'event');
    optCount(e, 'reviewedContentRevision', 'event', 1);
    optCount(e, 'attemptContentRevision', 'event', 1);
    oneOf(e, 'decision', 'event', DECISIONS, true);
    for (const k of ['actor', 'operationKey', 'recordedAt']) str(e, k, 'event');
    for (const k of ['attemptId', 'executorRef', 'artifactRef', 'evidenceRef', 'contentDigest', 'attemptPrereqDigest', 'claimKey']) optStr(e, k, 'event');
  }
  return o as unknown as EventPageView;
}

function checkTrace(v: unknown, nodeId: string, attemptId: string, afterSeq: number | null): AttemptTracePageView {
  const o = obj(v, 'trace page');
  checkVersion(o, 'The trace page');
  if (o.nodeId !== nodeId || o.attemptId !== attemptId)
    throw new ContractApiError(200, 'unexpected_shape', 'The trace is for a different node or attempt.');
  count(o, 'attemptEpoch', 'trace page');
  optStr(o, 'claimKey', 'trace page');
  oneOf(o, 'status', 'trace page', TRACE_STATUSES);
  oneOf(o, 'reason', 'trace page', TRACE_REASONS, true);
  oneOf(o, 'integrity', 'trace page', TRACE_INTEGRITY);
  optStr(o, 'executionKind', 'trace page');
  optCount(o, 'nextAfterSeq', 'trace page');
  bool(o, 'capped', 'trace page');
  if (o.exit !== null) {
    const x = obj(o.exit, 'trace page.exit');
    optCount(x, 'code', 'trace exit', Number.MIN_SAFE_INTEGER);
    optStr(x, 'killReason', 'trace exit');
  }
  if (o.prompt !== null) {
    const p = obj(o.prompt, 'trace page.prompt');
    str(p, 'text', 'trace prompt');
    count(p, 'bytes', 'trace prompt');
  }
  let previous = afterSeq ?? -1;
  const records = arr(o, 'records', 'trace page');
  for (const raw of records) {
    const r = obj(raw, 'trace record');
    count(r, 'seq', 'trace record');
    if ((r.seq as number) <= previous) bad('trace record.seq order');
    previous = r.seq as number;
    count(r, 'tMs', 'trace record');
    oneOf(r, 'stream', 'trace record', TRACE_STREAMS);
    str(r, 'text', 'trace record');
    bool(r, 'cut', 'trace record');
    bool(r, 'redacted', 'trace record');
  }
  // Cross-field rules of the contract: a status never contradicts its reason, integrity or exit.
  const notCaptured = o.status === 'not_captured';
  if (notCaptured !== (o.reason !== null)) bad('trace page.reason');
  if (notCaptured && (records.length > 0 || o.integrity !== 'none')) bad('trace page.status');
  if (o.integrity === 'verified' && o.status !== 'exited') bad('trace page.integrity');
  if (o.exit !== null && o.status !== 'exited') bad('trace page.exit');
  if (afterSeq !== null && o.prompt !== null) bad('trace page.prompt');
  return o as unknown as AttemptTracePageView;
}

// --- endpoints (GET only) -----------------------------------------------------

export async function listPlans(afterRootId: string | null, req: RequestGuard, limit = 100): Promise<PlanListPage> {
  const q = new URLSearchParams({ limit: String(limit) });
  if (afterRootId) q.set('afterRootId', afterRootId);
  return checkListPage((await getChecked(`/plans?${q}`, req)).value);
}

export async function getPlan(rootId: string, req: RequestGuard): Promise<PlanView> {
  return checkPlan((await getChecked(`/plans/${encodeURIComponent(rootId)}`, req)).value, rootId);
}

/** An event page plus the checked source text of its cursor (pass it back unchanged). */
export interface CheckedEventPage {
  page: EventPageView;
  nextCursorText: string | null;
}

async function events(path: string, requestedNode: string | null, cursorText: string | null, req: RequestGuard): Promise<CheckedEventPage> {
  const q = new URLSearchParams({ limit: '100' });
  if (cursorText !== null) q.set('afterSeq', cursorText);
  const checked = await getChecked(`${path}?${q}`, req, ['nextAfterSeq']);
  const page = checkEvents(checked.value, requestedNode);
  if (page.nextAfterSeq === null) return { page, nextCursorText: null };
  const text = checked.captured.nextAfterSeq;
  // The captured source text must denote exactly the parsed cursor (last-wins, escaped names included).
  if (text === undefined || BigInt(text) !== BigInt(page.nextAfterSeq)) bad('cursor');
  return { page, nextCursorText: text };
}

export const getPlanEvents = (rootId: string, cursorText: string | null, req: RequestGuard) =>
  events(`/plans/${encodeURIComponent(rootId)}/events`, null, cursorText, req);

export const getNodeEvents = (nodeId: string, cursorText: string | null, req: RequestGuard) =>
  events(`/nodes/${encodeURIComponent(nodeId)}/events`, nodeId, cursorText, req);

/**
 * One page of an attempt's trace. afterSeq is the last record seq already held (null for the
 * first page, which alone carries the prompt); it is a checked safe integer from an earlier page.
 */
export async function getAttemptTrace(nodeId: string, attemptId: string, afterSeq: number | null, req: RequestGuard): Promise<AttemptTracePageView> {
  const q = new URLSearchParams({ limit: '200' });
  if (afterSeq !== null) q.set('afterSeq', String(afterSeq));
  const path = `/nodes/${encodeURIComponent(nodeId)}/attempts/${encodeURIComponent(attemptId)}/trace?${q}`;
  return checkTrace((await getChecked(path, req)).value, nodeId, attemptId, afterSeq);
}
