// NodeDetail — read-only detail of one managed plan node (plan 021).
//
// Blockers are identified by (ownerId, predecessorId, gate, reason) — an inherited ancestor gate
// can block through a different owner — and are never merged with dependency edges, which are
// identified by (predecessorId, successorId).
//
// Depends on: planContract/types.ts, managed/labels.ts
// Used by: ManagedPlansView

import type { ReactNode } from 'react';
import type { PlanView } from '../../planContract/types';
import { blockerLabel } from './labels';

interface Props {
  plan: PlanView;
  nodeId: string;
  history: ReactNode;
}

export default function NodeDetail({ plan, nodeId, history }: Props) {
  const node = plan.nodes.find(n => n.id === nodeId);
  if (!node) return null;
  const nameOf = (id: string) => plan.nodes.find(n => n.id === id)?.name ?? id;
  const leaf = plan.readiness.leaves.find(l => l.nodeId === nodeId);
  const container = plan.readiness.containers.find(c => c.nodeId === nodeId);
  const incoming = plan.dependencies.filter(d => d.successorId === nodeId);
  const outgoing = plan.dependencies.filter(d => d.predecessorId === nodeId);
  const a = node.acceptance;

  return (
    <div data-testid="node-detail" data-node-id={node.id} className="text-sm space-y-3">
      <div>
        <div className="text-[10px] uppercase text-slate-500">{node.nodeType}</div>
        <div className="text-slate-100 font-medium">{node.name ?? node.id}</div>
        <div className="text-[11px] text-slate-500 font-mono">{node.id}</div>
      </div>

      <Section title="Content">
        <Field k="Content revision" v={node.contentRevision} />
        <Field k="Value" v={node.value} />
        {Object.entries(node.contentAttributes).sort(([x], [y]) => (x < y ? -1 : 1)).map(([k, v]) => <Field key={k} k={k} v={v} />)}
      </Section>

      {container ? (
        <Section title="Derived status (container)">
          <div data-testid="container-status" data-completion={container.completion} data-acceptance={container.acceptance}>
            <Field k="Completion" v={container.completion} />
            <Field k="Acceptance" v={container.acceptance} />
            <Field k="Gates hold" v={container.gatesHold ? 'yes' : 'no'} />
          </div>
          <div className="text-[11px] text-slate-500 mt-1">A container has no work or attempt of its own; this roll-up is computed by the API from its children.</div>
        </Section>
      ) : (
      <>
      <Section title="State">
        <Field k="Work" v={node.work} />
        <Field k="State revision" v={node.stateRevision} />
        <Field k="Attempt" v={node.attemptId === null ? null : `${node.attemptId} (epoch ${node.attemptEpoch})`} />
        <Field k="Artifact" v={node.artifactRef} />
        <Field k="Executor ref" v={node.executorRef} />
        <Field k="Pinned content revision" v={node.attemptContentRevision} />
        <Field k="Pinned prerequisite digest" v={node.attemptPrereqDigest} mono />
      </Section>

      <Section title="Acceptance">
        <Field k="Effective" v={node.effectiveAcceptance} testId="effective-acceptance" />
        {a ? (
          <div data-testid="raw-acceptance">
            <Field k="Recorded decision" v={a.decision} />
            <Field k="For content revision" v={a.contentRevision} />
            <Field k="For artifact" v={a.artifactRef} />
            <Field k="For attempt" v={`${a.attemptId ?? '—'} (epoch ${a.attemptEpoch})`} />
            <Field k="Decided by" v={a.decidedBy} />
            <Field k="Evidence" v={a.evidenceRef} />
          </div>
        ) : <div className="text-slate-500 text-xs">No recorded decision.</div>}
      </Section>
      </>
      )}

      {leaf && (
        <Section title={`Blockers (${leaf.blockers.length})`}>
          {leaf.blockers.length === 0 ? <div className="text-slate-500 text-xs">None.</div> : (
            <table className="w-full text-xs">
              <thead><tr className="text-slate-500 text-left"><th>Owner</th><th>Predecessor</th><th>Gate</th><th>Reason</th></tr></thead>
              <tbody>
                {leaf.blockers.map(b => {
                  const id = `${b.ownerId}|${b.predecessorId}|${b.gate}|${b.reason}`;
                  return (
                    <tr key={id} data-testid="blocker-row" data-blocker={id} className="text-slate-300">
                      <td>{b.ownerId === nodeId ? 'this node' : `${nameOf(b.ownerId)} (inherited)`}</td>
                      <td>{nameOf(b.predecessorId)}</td>
                      <td>{b.gate}</td>
                      <td title={b.reason}>{blockerLabel(b.reason)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Section>
      )}

      <Section title="Declared dependencies">
        <div className="text-xs text-slate-400">Depends on</div>
        {incoming.length === 0 ? <div className="text-slate-500 text-xs">None.</div> : incoming.map(d => (
          <div key={`${d.predecessorId}|${d.successorId}`} data-testid="dep-in" className="text-xs text-slate-300">
            {nameOf(d.predecessorId)} <span className="text-slate-500">gate {d.gate ?? `default (${plan.defaultGate})`}</span>
          </div>
        ))}
        <div className="text-xs text-slate-400 mt-1">Required by</div>
        {outgoing.length === 0 ? <div className="text-slate-500 text-xs">None.</div> : outgoing.map(d => (
          <div key={`${d.predecessorId}|${d.successorId}`} data-testid="dep-out" className="text-xs text-slate-300">
            {nameOf(d.successorId)} <span className="text-slate-500">gate {d.gate ?? `default (${plan.defaultGate})`}</span>
          </div>
        ))}
      </Section>

      <Section title="History">{history}</Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-1">{title}</div>
      {children}
    </div>
  );
}

function Field({ k, v, mono, testId }: { k: string; v: string | number | null; mono?: boolean; testId?: string }) {
  return (
    <div className="flex gap-2 text-xs" data-testid={testId}>
      <span className="text-slate-500 w-44 flex-shrink-0">{k}</span>
      <span className={`text-slate-200 break-all ${mono ? 'font-mono' : ''}`}>{v === null ? '—' : String(v)}</span>
    </div>
  );
}
