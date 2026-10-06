// PlanTree — expandable hierarchy of a managed plan (plan 021). Read-only.
//
// Statuses are displayed exactly as the API returned them (work, effective acceptance, ready,
// upstream changed, container roll-ups); nothing is re-derived here.
//
// Depends on: planContract/types.ts, managed/labels.ts
// Used by: ManagedPlansView

import { useState } from 'react';
import type { ContainerStatusView, LeafReadinessView, PlanNodeView, PlanView } from '../../planContract/types';
import { ACCEPTANCE_COLORS, WORK_COLORS } from './labels';

interface Props {
  plan: PlanView;
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export default function PlanTree({ plan, selectedId, onSelect }: Props) {
  const kidsOf = new Map<string, PlanNodeView[]>();
  for (const n of plan.nodes) {
    if (n.id === plan.rootId || n.parentId === null) continue;
    const list = kidsOf.get(n.parentId) ?? [];
    list.push(n);
    kidsOf.set(n.parentId, list);
  }
  for (const list of kidsOf.values())
    list.sort((a, b) => a.siblingOrder - b.siblingOrder || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const leaves = new Map(plan.readiness.leaves.map(l => [l.nodeId, l]));
  const containers = new Map(plan.readiness.containers.map(c => [c.nodeId, c]));
  const root = plan.nodes.find(n => n.id === plan.rootId);
  if (!root) return <div className="text-red-400 text-sm">The plan root is missing from the response.</div>;

  return (
    <ul data-testid="plan-tree" className="text-sm">
      <TreeRow node={root} depth={0} kidsOf={kidsOf} leaves={leaves} containers={containers}
        selectedId={selectedId} onSelect={onSelect} />
    </ul>
  );
}

interface RowProps {
  node: PlanNodeView;
  depth: number;
  kidsOf: Map<string, PlanNodeView[]>;
  leaves: Map<string, LeafReadinessView>;
  containers: Map<string, ContainerStatusView>;
  selectedId: string | null;
  onSelect: (id: string) => void;
}

function TreeRow({ node, depth, kidsOf, leaves, containers, selectedId, onSelect }: RowProps) {
  const [open, setOpen] = useState(depth < 2);
  const kids = kidsOf.get(node.id) ?? [];
  const leaf = leaves.get(node.id);
  const container = containers.get(node.id);
  return (
    <li>
      <div
        data-testid={`tree-node-${node.id}`}
        data-work={leaf ? leaf.work : undefined}
        data-effective={leaf ? node.effectiveAcceptance : undefined}
        data-ready={leaf ? String(leaf.ready) : undefined}
        data-completion={container?.completion}
        data-acceptance={container?.acceptance}
        className={`flex items-center gap-2 py-0.5 pr-2 rounded cursor-pointer hover:bg-slate-700/50 ${
          selectedId === node.id ? 'bg-slate-700' : ''}`}
        style={{ paddingLeft: depth * 16 + 4 }}
        onClick={() => onSelect(node.id)}
      >
        {kids.length > 0 ? (
          <button
            data-testid={`tree-toggle-${node.id}`}
            aria-label={open ? 'Collapse' : 'Expand'}
            className="w-4 text-slate-400 hover:text-slate-100"
            onClick={e => { e.stopPropagation(); setOpen(o => !o); }}
          >{open ? '▾' : '▸'}</button>
        ) : <span className="w-4" />}
        <span className="text-[10px] uppercase text-slate-500">{node.nodeType}</span>
        <span className="text-slate-200 truncate">{node.name ?? node.id}</span>
        {leaf && (
          <>
            <span className={`text-xs ${WORK_COLORS[leaf.work] ?? 'text-slate-300'}`}>{leaf.work}</span>
            <span className={`text-xs ${ACCEPTANCE_COLORS[node.effectiveAcceptance] ?? 'text-slate-300'}`}>{node.effectiveAcceptance}</span>
            <span className={`text-xs ${leaf.ready ? 'text-emerald-300' : 'text-slate-500'}`}>{leaf.ready ? 'ready' : 'not ready'}</span>
            {leaf.upstreamChanged && <span className="text-xs text-amber-400">upstream changed</span>}
            {leaf.attemptId && <span className="text-xs text-slate-400">attempt {leaf.attemptId}/{leaf.attemptEpoch}</span>}
          </>
        )}
        {container && (
          <>
            <span className="text-xs text-slate-300">{container.completion}</span>
            <span className={`text-xs ${ACCEPTANCE_COLORS[container.acceptance] ?? 'text-slate-300'}`}>{container.acceptance}</span>
            {!container.gatesHold && <span className="text-xs text-amber-400">gates do not hold</span>}
          </>
        )}
      </div>
      {open && kids.length > 0 && (
        <ul>
          {kids.map(k => (
            <TreeRow key={k.id} node={k} depth={depth + 1} kidsOf={kidsOf} leaves={leaves} containers={containers}
              selectedId={selectedId} onSelect={onSelect} />
          ))}
        </ul>
      )}
    </li>
  );
}
