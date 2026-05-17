/**
 * Node kinds come in two flavors:
 *
 * - **Behavioral** — describes what the node *does* at runtime. The planning
 *   agent reaches for these primitives (tool_call before commit, check
 *   before irreversible action, sub_agent for delegated reasoning).
 * - **Structural** — mirrors the context store's plan ontology so the viewer
 *   can render real plans directly. plan → plan_phase → task is the natural
 *   containment hierarchy; question and risk hang as siblings.
 *
 * These coexist on the same union so the Graph data model is one type
 * regardless of who's writing into it (agent, plan source, executor).
 */
export type NodeKind =
  // behavioral
  | 'tool_call'
  | 'sub_agent'
  | 'transform'
  | 'check'
  // structural (context store)
  | 'plan'
  | 'plan_phase'
  | 'task'
  | 'question'
  | 'risk'

export interface Node {
  id: string
  kind: NodeKind
  config: Record<string, unknown>
  intent: string
  /**
   * Containment parent. A node optionally lives *inside* another node —
   * a phase contains tasks, a service contains components, etc. Flow edges
   * (the `connect` API) cross containment boundaries freely; containment
   * is the structural lens, flow is the causal one.
   */
  parentId?: string
  status?: NodeStatus
  result?: string
  error?: string
}

export type NodeStatus =
  | 'planned'
  | 'ready'
  | 'running'
  | 'done'
  | 'failed'

export type Port = 'out' | 'result' | 'error' | 'pass' | 'fail'

export interface Edge {
  fromId: string
  toId: string
  fromPort: Port
}

export class Graph {
  readonly nodes = new Map<string, Node>()
  readonly edges: Edge[] = []

  addNode(node: Node): void {
    if (node.parentId !== undefined && !this.nodes.has(node.parentId)) {
      throw new Error(`unknown containment parent '${node.parentId}'`)
    }
    this.nodes.set(node.id, { status: 'planned', ...node })
  }

  setStatus(
    id: string,
    status: NodeStatus,
    extra: { result?: string; error?: string } = {},
  ): void {
    const n = this.nodes.get(id)
    if (!n) throw new Error(`unknown node '${id}'`)
    n.status = status
    if (extra.result !== undefined) n.result = extra.result
    if (extra.error !== undefined) n.error = extra.error
  }

  /** Direct containment children of `id`, in insertion order. */
  contained(id: string): string[] {
    const out: string[] = []
    for (const [nid, n] of this.nodes) if (n.parentId === id) out.push(nid)
    return out
  }

  /** Top-level nodes — no containment parent — in insertion order. */
  containmentRoots(): string[] {
    const out: string[] = []
    for (const [nid, n] of this.nodes) if (n.parentId === undefined) out.push(nid)
    return out
  }

  /** Containment depth — 0 for a root, 1 for a direct child of a root, etc. */
  depth(id: string): number {
    let d = 0
    let cur = this.nodes.get(id)?.parentId
    while (cur !== undefined) {
      d += 1
      cur = this.nodes.get(cur)?.parentId
    }
    return d
  }

  connect(fromId: string, toId: string, fromPort: Port = 'out'): void {
    if (!this.nodes.has(fromId)) throw new Error(`unknown source node '${fromId}'`)
    if (!this.nodes.has(toId)) throw new Error(`unknown target node '${toId}'`)
    this.edges.push({ fromId, toId, fromPort })
  }
}
