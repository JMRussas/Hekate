// Display labels for plan-contract codes (plan 021). Codes are shown as-is next to the label;
// unknown codes fall back to the raw code, never to a guess.
//
// Depends on: nothing
// Used by: components/managed/*

export const BLOCKER_REASONS: Record<string, string> = {
  predecessor_not_completed: 'Predecessor is not done',
  predecessor_not_accepted: 'Predecessor is not accepted',
  predecessor_acceptance_stale: 'Predecessor acceptance is stale',
  predecessor_rejected: 'Predecessor was rejected',
  predecessor_cancelled: 'Predecessor was cancelled',
  predecessor_upstream_changed: "Predecessor's own gates no longer hold",
};

export const blockerLabel = (reason: string) => BLOCKER_REASONS[reason] ?? reason;

export const WORK_COLORS: Record<string, string> = {
  todo: 'text-slate-400',
  in_progress: 'text-blue-400',
  done: 'text-emerald-400',
  cancelled: 'text-slate-500 line-through',
};

const TRACE_NOT_CAPTURED: Record<string, string> = {
  no_trace_block: 'No trace was recorded for this attempt.',
  no_claim_receipt: 'No trace was recorded: the attempt has no claim receipt.',
  journal_compacted: 'No trace is available: the attempt’s journal records were compacted.',
};

/** What is known about an attempt's trace (trace contract v0) — not process liveness, not acceptance. */
export function traceStatusLabel(status: string, reason: string | null, integrity: string): string {
  if (status === 'not_captured') return TRACE_NOT_CAPTURED[reason ?? ''] ?? 'No trace was recorded for this attempt.';
  if (status === 'running') return 'Attempt in progress; trace unverified.';
  if (status === 'unfinished') return 'Attempt ended without a final record; trace unverified.';
  if (status === 'missing') return 'Trace file missing.';
  if (status === 'exited') return integrity === 'verified' ? 'Attempt finished; trace matches its recorded hash.' : 'Attempt finished; trace unverified.';
  return status;
}

export const TRACE_ERRORS: Record<string, string> = {
  TRACE_INTEGRITY_MISMATCH: 'The trace does not match its recorded hash, so it is not shown.',
  TRACE_PATH_REFUSED: 'The trace path is outside the allowed runs root, so it is not shown.',
  TRACE_IDENTITY_CONFLICT: 'The attempt’s claim records disagree, so no trace is shown.',
  ATTEMPT_NOT_FOUND: 'This attempt is not recorded for the node.',
  NODE_NOT_FOUND: 'The node was not found.',
};

export const ACCEPTANCE_COLORS: Record<string, string> = {
  accepted: 'text-emerald-400',
  rejected: 'text-red-400',
  stale: 'text-amber-400 font-semibold',
  none: 'text-slate-500',
  pending: 'text-slate-400',
  empty: 'text-slate-500',
};
