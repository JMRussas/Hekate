// Plan contract v1 read models (plan 021) — mirror context-store/Api/PlanContractEndpoints.cs
//
// Every integer here was checked by parseContractJson to be a safe JSON integer, so the
// `number` values are exact. Enum-like fields are the API's snake_case names, passed through
// verbatim (never re-derived in the UI).
//
// Depends on: nothing
// Used by: planContract/api.ts, ManagedPlansView and components/managed/*

export const SUPPORTED_CONTRACT_VERSION = 'plan-contract/v1';

export interface PlanListItem {
  rootId: string;
  projectId: string;
  name: string | null;
  defaultGate: string;
  contractVersion: string;
  supported: boolean;
  createdAt: string;
  createdBy: string;
  eventSeq: number;
}

export interface PlanListPage {
  contractVersion: string;
  plans: PlanListItem[];
  nextAfterRootId: string | null;
}

export interface AcceptanceView {
  decision: string;
  contentRevision: number;
  artifactRef: string | null;
  attemptId: string | null;
  attemptEpoch: number;
  decidedBy: string;
  evidenceRef: string | null;
}

export interface PlanNodeView {
  id: string;
  parentId: string | null;
  nodeType: string;
  name: string | null;
  value: string | null;
  contentAttributes: Record<string, string>;
  siblingOrder: number;
  contentRevision: number;
  stateRevision: number;
  work: string;
  attemptId: string | null;
  attemptEpoch: number;
  artifactRef: string | null;
  executorRef: string | null;
  attemptContentRevision: number | null;
  attemptPrereqDigest: string | null;
  acceptance: AcceptanceView | null;
  effectiveAcceptance: string;
}

export interface DependencyView {
  predecessorId: string;
  successorId: string;
  gate: string | null;
}

export interface BlockerView {
  ownerId: string;
  predecessorId: string;
  gate: string;
  reason: string;
}

export interface LeafReadinessView {
  nodeId: string;
  name: string | null;
  work: string;
  ready: boolean;
  gatesHold: boolean;
  upstreamChanged: boolean;
  attemptId: string | null;
  attemptEpoch: number;
  blockers: BlockerView[];
}

export interface ContainerStatusView {
  nodeId: string;
  name: string | null;
  completion: string;
  acceptance: string;
  gatesHold: boolean;
}

export interface ContractErrorView {
  code: string;
  message: string;
  nodeId: string | null;
  relatedId: string | null;
}

export interface ReadinessView {
  contractVersion: string;
  rootId: string;
  errors: ContractErrorView[];
  leaves: LeafReadinessView[];
  containers: ContainerStatusView[];
}

export interface PlanView {
  contractVersion: string;
  outcome: string | null;
  rootId: string;
  projectId: string;
  defaultGate: string;
  nodes: PlanNodeView[];
  dependencies: DependencyView[];
  readiness: ReadinessView;
}

export interface PlanEventView {
  seq: number;
  nodeId: string;
  nodeStateRevision: number;
  kind: string;
  workFrom: string;
  workTo: string;
  contentRevision: number;
  attemptId: string | null;
  attemptEpoch: number;
  executorRef: string | null;
  artifactRef: string | null;
  decision: string | null;
  reviewedContentRevision: number | null;
  evidenceRef: string | null;
  contentDigest: string | null;
  actor: string;
  operationKey: string;
  recordedAt: string;
  attemptContentRevision: number | null;
  attemptPrereqDigest: string | null;
  claimKey: string | null;
}

export interface EventPageView {
  contractVersion: string;
  events: PlanEventView[];
  nextAfterSeq: number | null;
  historyStartsAtSeq: number | null;
  historyBackfilled: boolean;
}
