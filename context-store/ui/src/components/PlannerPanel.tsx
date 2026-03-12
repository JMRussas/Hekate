// PlannerPanel — plan list and tree viewer
//
// Shows all plans as cards, click to drill into recursive tree view.
// Supports expand/collapse with node type icons and status colors.
// Depends on: api.ts
// Used by: App.tsx

import { useState, useEffect } from 'react';
import { type PlanSummary, type PlanNode, listPlans, getPlan } from '../api';

const typeIcons: Record<string, string> = {
  plan_phase: '\u25C6',
  plan_step: '\u25B8',
  task: '\u00B7',
  risk: '\u26A0',
  question: '?',
  decision: '\u2192',
  test_spec: '\u2713',
  blocker: '\u2715',
  milestone: '\u25CE',
  retrospective: '\u21A9',
};

const statusTextColors: Record<string, string> = {
  completed: 'text-green-400',
  done: 'text-green-400',
  in_progress: 'text-blue-400',
  pending: 'text-slate-400',
  blocked: 'text-red-400',
  blocking: 'text-red-400',
  proposed: 'text-amber-400',
};

const planTypeBadge: Record<string, string> = {
  feature: 'bg-blue-600/20 border-blue-500/30 text-blue-300',
  bugfix: 'bg-red-600/20 border-red-500/30 text-red-300',
  roadmap: 'bg-purple-600/20 border-purple-500/30 text-purple-300',
  spike: 'bg-amber-600/20 border-amber-500/30 text-amber-300',
};

const statusBadge: Record<string, string> = {
  pending: 'bg-yellow-500/10 border-yellow-500/30 text-yellow-300',
  in_progress: 'bg-blue-500/10 border-blue-500/30 text-blue-300',
  completed: 'bg-green-500/10 border-green-500/30 text-green-300',
  done: 'bg-green-500/10 border-green-500/30 text-green-300',
  proposed: 'bg-amber-500/10 border-amber-500/30 text-amber-300',
  blocked: 'bg-red-500/10 border-red-500/30 text-red-300',
};

const priorityBadge: Record<string, string> = {
  p0: 'text-red-400',
  p1: 'text-amber-400',
  p2: 'text-slate-400',
};

function StatusBadge({ status }: { status: string | null }) {
  if (!status) return null;
  return (
    <span className={`border text-[10px] px-1.5 py-0.5 rounded ${statusBadge[status] || statusBadge.pending}`}>
      {status}
    </span>
  );
}

function PlanTypeBadge({ planType }: { planType: string | null }) {
  if (!planType) return null;
  return (
    <span className={`border text-[10px] px-1.5 py-0.5 rounded ${planTypeBadge[planType] || planTypeBadge.feature}`}>
      {planType}
    </span>
  );
}

// --- Tree node ---

function TreeNode({ node, depth }: { node: PlanNode; depth: number }) {
  const [expanded, setExpanded] = useState(depth < 2);
  const hasChildren = node.children.length > 0;
  const icon = typeIcons[node.nodeType] || '\u25CB';
  const statusColor = statusTextColors[node.status || ''] || 'text-slate-400';

  return (
    <div style={{ paddingLeft: depth * 12 }}>
      <button
        onClick={() => hasChildren && setExpanded(!expanded)}
        className={`flex items-center gap-1.5 w-full text-left py-0.5 group ${
          hasChildren ? 'cursor-pointer' : 'cursor-default'
        }`}
      >
        {hasChildren && (
          <span className="text-[10px] text-slate-500 w-3 text-center">
            {expanded ? '\u25BC' : '\u25B6'}
          </span>
        )}
        {!hasChildren && <span className="w-3" />}
        <span className={`text-sm ${statusColor}`}>{icon}</span>
        <span className="text-sm text-slate-200 truncate flex-1">
          {node.name || node.nodeType}
        </span>
        {node.status && <StatusBadge status={node.status} />}
      </button>
      {expanded && hasChildren && (
        <div>
          {node.children.map((child) => (
            <TreeNode key={child.id} node={child} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

// --- Main component ---

export default function PlannerPanel() {
  const [plans, setPlans] = useState<PlanSummary[]>([]);
  const [selectedPlan, setSelectedPlan] = useState<PlanNode | null>(null);
  const [selectedSummary, setSelectedSummary] = useState<PlanSummary | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [treeLoading, setTreeLoading] = useState(false);

  useEffect(() => {
    listPlans()
      .then(setPlans)
      .catch(() => setPlans([]))
      .finally(() => setListLoading(false));
  }, []);

  const handleSelect = async (plan: PlanSummary) => {
    setTreeLoading(true);
    try {
      const tree = await getPlan(plan.id);
      setSelectedPlan(tree);
      setSelectedSummary(plan);
    } catch {
      setSelectedPlan(null);
    } finally {
      setTreeLoading(false);
    }
  };

  const handleBack = () => {
    setSelectedPlan(null);
    setSelectedSummary(null);
  };

  // --- Tree view ---
  if (selectedPlan) {
    return (
      <div className="h-full overflow-y-auto p-3">
        <button
          onClick={handleBack}
          className="text-sm text-slate-400 hover:text-slate-200 mb-3 transition-colors"
        >
          &larr; Plans
        </button>

        {/* Plan header */}
        <div className="mb-3">
          <h2 className="text-sm font-semibold text-slate-200 mb-1">
            {selectedSummary?.name || selectedPlan.name || 'Plan'}
          </h2>
          <div className="flex items-center gap-1.5 flex-wrap">
            <PlanTypeBadge planType={selectedSummary?.planType || null} />
            <StatusBadge status={selectedSummary?.status || selectedPlan.status} />
          </div>
        </div>

        {/* Tree */}
        <div className="border-t border-slate-700 pt-2">
          {treeLoading && <p className="text-sm text-slate-500">Loading tree...</p>}
          {!treeLoading && selectedPlan.children.length > 0 ? (
            selectedPlan.children.map((child) => (
              <TreeNode key={child.id} node={child} depth={0} />
            ))
          ) : (
            <p className="text-sm text-slate-500 italic">No phases or steps</p>
          )}
        </div>
      </div>
    );
  }

  // --- List view ---
  return (
    <div className="h-full overflow-y-auto p-3">
      <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Plans</h2>

      {listLoading && <p className="text-sm text-slate-500">Loading...</p>}

      {!listLoading && plans.length === 0 && (
        <p className="text-sm text-slate-500 italic">No plans found</p>
      )}

      <div className="space-y-2">
        {plans.map((plan) => (
          <button
            key={plan.id}
            onClick={() => handleSelect(plan)}
            className="w-full text-left border border-slate-700/50 rounded-lg p-2 hover:border-slate-600 transition-colors"
          >
            <div className="text-sm font-medium text-slate-200 truncate">{plan.name}</div>
            <div className="flex items-center gap-1.5 mt-1 flex-wrap">
              <PlanTypeBadge planType={plan.planType} />
              <StatusBadge status={plan.status} />
              {plan.priority && (
                <span className={`text-[10px] font-medium ${priorityBadge[plan.priority] || 'text-slate-400'}`}>
                  {plan.priority.toUpperCase()}
                </span>
              )}
            </div>
            {plan.value && (
              <p className="text-xs text-slate-400 mt-1 line-clamp-2">{plan.value}</p>
            )}
            <div className="text-[10px] text-slate-500 mt-1">
              {plan.phaseCount} phase{plan.phaseCount !== 1 ? 's' : ''}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
