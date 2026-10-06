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

export const ACCEPTANCE_COLORS: Record<string, string> = {
  accepted: 'text-emerald-400',
  rejected: 'text-red-400',
  stale: 'text-amber-400 font-semibold',
  none: 'text-slate-500',
  pending: 'text-slate-400',
  empty: 'text-slate-500',
};
