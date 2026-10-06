// Deterministic layering for the dependency map (plan 021). Pure functions, no React.
//
// Depends on: nothing
// Used by: managed/DependencyMap.tsx

export const MAP_NODE_LIMIT = 200;

export const byId = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);

/** Longest-path layers; null when the edges contain a cycle (the server rejects those, but never trust it). */
export function layers(nodeIds: string[], edges: { predecessorId: string; successorId: string }[]): Map<string, number> | null {
  const preds = new Map<string, string[]>(nodeIds.map(id => [id, []]));
  for (const e of edges) preds.get(e.successorId)?.push(e.predecessorId);
  const layer = new Map<string, number>();
  const visiting = new Set<string>();
  let cyclic = false;
  const visit = (id: string): number => {
    const known = layer.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) { cyclic = true; return 0; }
    visiting.add(id);
    let l = 0;
    for (const p of preds.get(id) ?? []) if (preds.has(p)) l = Math.max(l, visit(p) + 1);
    visiting.delete(id);
    layer.set(id, l);
    return l;
  };
  for (const id of [...nodeIds].sort(byId)) visit(id);
  return cyclic ? null : layer;
}
