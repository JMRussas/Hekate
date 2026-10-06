// DependencyMap — deterministic layered SVG of declared dependencies (plan 021). Read-only.
//
// Layer = longest path over declared edges (predecessor -> successor); within a layer nodes are
// ordered by id. Every declared edge is drawn once, identified by (predecessorId, successorId);
// an edge is highlighted as blocking when a blocker with ownerId = successor and the same
// predecessor exists. Above MAP_NODE_LIMIT nodes, or if the edges contain a cycle, a table is
// shown instead. No layout library.
//
// Depends on: planContract/types.ts, managed/layout.ts
// Used by: ManagedPlansView

import type { PlanView } from '../../planContract/types';
import { MAP_NODE_LIMIT, byId, layers } from './layout';

const COL_W = 220;
const ROW_H = 52;
const BOX_W = 170;
const BOX_H = 38;

interface Props {
  plan: PlanView;
  selectedId: string | null;
  onSelect: (id: string) => void;
}



export default function DependencyMap({ plan, selectedId, onSelect }: Props) {
  const nodes = plan.nodes.filter(n => n.id !== plan.rootId);
  const nameOf = (id: string) => plan.nodes.find(n => n.id === id)?.name ?? id;
  const blocked = new Set<string>();
  for (const l of plan.readiness.leaves) for (const b of l.blockers) blocked.add(`${b.predecessorId}|${b.ownerId}`);
  const edges = [...plan.dependencies].sort((a, b) => byId(a.predecessorId, b.predecessorId) || byId(a.successorId, b.successorId));
  const layerOf = nodes.length <= MAP_NODE_LIMIT ? layers(nodes.map(n => n.id), edges) : null;

  if (layerOf === null) {
    return (
      <div>
        <div className="text-xs text-slate-400 mb-2">
          {nodes.length > MAP_NODE_LIMIT ? `${nodes.length} nodes exceed the map limit (${MAP_NODE_LIMIT}); showing the edge table.` : 'Dependencies are cyclic; showing the edge table.'}
        </div>
        <table data-testid="dependency-table" className="w-full text-xs text-slate-300">
          <thead><tr className="text-slate-500 text-left"><th>Predecessor</th><th>Successor</th><th>Gate</th><th>Blocking</th></tr></thead>
          <tbody>
            {edges.map(e => {
              const id = `${e.predecessorId}|${e.successorId}`;
              return (
                <tr key={id} data-edge={id} data-blocked={String(blocked.has(id))}>
                  <td>{nameOf(e.predecessorId)}</td><td>{nameOf(e.successorId)}</td>
                  <td>{e.gate ?? `default (${plan.defaultGate})`}</td><td>{blocked.has(id) ? 'yes' : ''}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  }

  const columns = new Map<number, string[]>();
  for (const n of nodes) {
    const l = layerOf.get(n.id)!;
    columns.set(l, [...(columns.get(l) ?? []), n.id]);
  }
  const pos = new Map<string, { x: number; y: number }>();
  for (const [l, ids] of columns) ids.sort(byId).forEach((id, i) => pos.set(id, { x: 10 + l * COL_W, y: 10 + i * ROW_H }));
  const width = 20 + Math.max(0, ...[...columns.keys()]) * COL_W + BOX_W;
  const height = 20 + Math.max(1, ...[...columns.values()].map(c => c.length)) * ROW_H;
  // Status lines come from the API: containers use their derived roll-up (never the stored
  // node work, which is always todo for a container); leaves show work and effective acceptance.
  const containerStatus = new Map(plan.readiness.containers.map(c => [c.nodeId, c]));
  const statusLine = (id: string, work: string, effective: string) => {
    const c = containerStatus.get(id);
    return c ? `${c.completion} · ${c.acceptance}${c.gatesHold ? "" : " · gates do not hold"}` : `${work} · ${effective}`;
  };

  return (
    <svg data-testid="dependency-map" width={width} height={height} className="bg-slate-900">
      {edges.map(e => {
        const a = pos.get(e.predecessorId);
        const b = pos.get(e.successorId);
        if (!a || !b) return null;
        const id = `${e.predecessorId}|${e.successorId}`;
        const isBlocked = blocked.has(id);
        const gate = e.gate ?? plan.defaultGate;
        return (
          <line key={id} data-edge={id} data-blocked={String(isBlocked)} data-gate={gate}
            x1={a.x + BOX_W} y1={a.y + BOX_H / 2} x2={b.x} y2={b.y + BOX_H / 2}
            stroke={isBlocked ? '#f87171' : gate === 'accepted' ? '#34d399' : '#38bdf8'}
            strokeWidth={isBlocked ? 2 : 1.25} strokeDasharray={e.gate === null ? '4 3' : undefined} />
        );
      })}
      {nodes.map(n => {
        const p = pos.get(n.id)!;
        return (
          <g key={n.id} data-map-node={n.id} data-layer={layerOf.get(n.id)} onClick={() => onSelect(n.id)} className="cursor-pointer">
            <rect x={p.x} y={p.y} width={BOX_W} height={BOX_H} rx={4}
              fill={selectedId === n.id ? '#334155' : '#1e293b'} stroke={containerStatus.has(n.id) ? '#94a3b8' : '#475569'}
              strokeDasharray={containerStatus.has(n.id) ? '3 2' : undefined} />
            <title>{`${n.name ?? n.id}\n${statusLine(n.id, n.work, n.effectiveAcceptance)}`}</title>
            <text x={p.x + 6} y={p.y + 15} fontSize={11} fill="#e2e8f0">{(n.name ?? n.id).slice(0, 26)}</text>
            <text data-status-line={statusLine(n.id, n.work, n.effectiveAcceptance)} x={p.x + 6} y={p.y + 30} fontSize={10}
              fill={n.effectiveAcceptance === 'stale' && !containerStatus.has(n.id) ? '#fbbf24' : '#94a3b8'}>
              {statusLine(n.id, n.work, n.effectiveAcceptance)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
