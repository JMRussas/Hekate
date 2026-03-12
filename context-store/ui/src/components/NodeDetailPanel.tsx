// NodeDetailPanel — shows full detail for a focused node
//
// Breadcrumb, type badge, name, value, attributes, graph edges, children list.
// Edit mode for inline modifications. Create child node form.
// Clicking a breadcrumb or child navigates to that node.
//
// Depends on: api.ts (getNodeDetail, getNodeChildren, getNodeEdges, updateNode, updateAttributes, createChildNode)
// Used by: WorkspacePanel

import { useState, useEffect } from 'react';
import {
  type NodeDetail,
  type NodeChild,
  type NodeEdge,
  getNodeDetail,
  getNodeChildren,
  getNodeEdges,
  updateNode,
  updateAttributes,
  createChildNode,
  materializeCode,
} from '../api';
import { TYPE_COLORS, STATUS_COLORS, EDGE_ICONS } from '../colors';

interface Props {
  nodeId: string;
  onNavigate: (nodeId: string) => void;
  onNodeChanged?: () => void;
}

interface EditableAttribute {
  key: string;
  value: string;
}

export default function NodeDetailPanel({ nodeId, onNavigate, onNodeChanged }: Props) {
  const [detail, setDetail] = useState<NodeDetail | null>(null);
  const [children, setChildren] = useState<NodeChild[]>([]);
  const [edges, setEdges] = useState<NodeEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [showValue, setShowValue] = useState(false);

  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState('');
  const [editValue, setEditValue] = useState('');
  const [editAttributes, setEditAttributes] = useState<EditableAttribute[]>([]);
  const [saving, setSaving] = useState(false);

  const [showCreateChild, setShowCreateChild] = useState(false);
  const [newChildType, setNewChildType] = useState('idea');
  const [newChildName, setNewChildName] = useState('');
  const [newChildValue, setNewChildValue] = useState('');
  const [creating, setCreating] = useState(false);

  const [materializedSource, setMaterializedSource] = useState<string | null>(null);
  const [materializing, setMaterializing] = useState(false);

  const isCodeNode = (type: string) =>
    ['compilation_unit', 'namespace', 'class', 'struct', 'method', 'constructor'].includes(type);

  const handleMaterialize = async () => {
    if (!detail) return;
    setMaterializing(true);
    try {
      const result = await materializeCode(undefined, nodeId);
      setMaterializedSource(result.source);
    } catch (err) {
      console.error('Materialize failed:', err);
    } finally {
      setMaterializing(false);
    }
  };

  const loadNode = async (id: string) => {
    const [d, c, e] = await Promise.all([
      getNodeDetail(id),
      getNodeChildren(id),
      getNodeEdges(id),
    ]);
    setDetail(d);
    setChildren(c);
    setEdges(e);
    return d;
  };

  useEffect(() => {
    setLoading(true);
    setShowValue(false);
    setEditing(false);
    setSaving(false);
    setShowCreateChild(false);
    setCreating(false);
    setMaterializedSource(null);
    setMaterializing(false);
    setNewChildType('idea');
    setNewChildName('');
    setNewChildValue('');

    loadNode(nodeId)
      .catch((err) => console.error('Node detail failed:', err))
      .finally(() => setLoading(false));
  }, [nodeId]);

  const startEditing = () => {
    if (!detail) return;
    setEditName(detail.node.name || '');
    setEditValue(detail.node.value || '');
    setEditAttributes(Object.entries(detail.attributes).map(([key, value]) => ({ key, value })));
    setEditing(true);
  };

  const handleSave = async () => {
    if (!detail) return;
    setSaving(true);
    try {
      const trimmedName = editName.trim();
      const nextAttributes: Record<string, string> = {};

      for (const item of editAttributes) {
        const key = item.key.trim();
        if (!key) continue;
        nextAttributes[key] = item.value;
      }

      await updateNode(nodeId, {
        name: trimmedName ? trimmedName : null,
        value: editValue.trim() ? editValue : null,
      });
      await updateAttributes(nodeId, nextAttributes);
      await loadNode(nodeId);

      setEditing(false);
      onNodeChanged?.();
    } catch (err) {
      console.error('Failed to save node:', err);
    } finally {
      setSaving(false);
    }
  };

  const handleCreateChild = async () => {
    if (creating) return;
    setCreating(true);
    try {
      await createChildNode(nodeId, {
        nodeType: newChildType,
        name: newChildName.trim() ? newChildName.trim() : null,
        value: newChildValue.trim() ? newChildValue : null,
      });

      const [d, c] = await Promise.all([
        getNodeDetail(nodeId),
        getNodeChildren(nodeId),
      ]);
      setDetail(d);
      setChildren(c);
      setNewChildType('idea');
      setNewChildName('');
      setNewChildValue('');
      onNodeChanged?.();
    } catch (err) {
      console.error('Failed to create child node:', err);
    } finally {
      setCreating(false);
    }
  };

  if (loading) {
    return (
      <div className="p-4 text-slate-400 text-sm animate-pulse">
        Loading node...
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="p-4 text-red-400 text-sm">Node not found</div>
    );
  }

  const { node, attributes, breadcrumb, childCount } = detail;
  const status = attributes.status;

  return (
    <div className="flex flex-col h-full overflow-y-auto">
      {/* Breadcrumb */}
      {breadcrumb.length > 0 && (
        <div className="px-3 py-2 border-b border-slate-700 flex items-center gap-1 text-xs text-slate-400 flex-wrap">
          {breadcrumb.map((crumb, i) => (
            <span key={crumb.id} className="flex items-center gap-1">
              {i > 0 && <span className="text-slate-600">›</span>}
              <button
                onClick={() => onNavigate(crumb.id)}
                className="hover:text-slate-200 transition-colors"
              >
                {crumb.name || crumb.nodeType}
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Header — type badge + name + status */}
      <div className="px-4 py-3 border-b border-slate-700">
        <div className="flex items-center gap-2 mb-2">
          <span className={`${TYPE_COLORS[node.nodeType] || 'bg-gray-600'} text-xs px-2 py-0.5 rounded-full font-medium`}>
            {node.nodeType}
          </span>
          {status && (
            <span className={`text-xs ${STATUS_COLORS[status] || 'text-slate-400'}`}>
              {status}
            </span>
          )}
        </div>

        {editing ? (
          <div className="space-y-2">
            <input
              type="text"
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              placeholder="Node name"
              className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1 text-sm text-slate-200
                         placeholder-slate-500 focus:outline-none focus:border-blue-500"
            />
            <div className="flex items-center gap-2">
              <button
                onClick={handleSave}
                disabled={saving}
                className="bg-blue-600 hover:bg-blue-500 disabled:bg-slate-700 disabled:text-slate-500
                           text-xs px-2.5 py-1 rounded font-medium transition-colors"
              >
                {saving ? 'Saving...' : 'Save'}
              </button>
              <button
                onClick={() => setEditing(false)}
                disabled={saving}
                className="bg-slate-700 hover:bg-slate-600 border border-slate-600 disabled:opacity-50
                           text-xs px-2.5 py-1 rounded text-slate-200 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-slate-200">
              {node.name || '(unnamed)'}
            </h2>
            <button
              onClick={startEditing}
              className="bg-slate-700 hover:bg-slate-600 border border-slate-600
                         text-xs px-2 py-1 rounded text-slate-200 transition-colors"
            >
              Edit
            </button>
          </div>
        )}

        <div className="text-xs text-slate-500 mt-1">
          {new Date(node.createdAt).toLocaleString()}
          {node.modifiedBy && <span> · by {node.modifiedBy}</span>}
        </div>
      </div>

      {/* Value */}
      {editing ? (
        <div className="px-4 py-2 border-b border-slate-700">
          <h3 className="text-xs font-medium text-slate-400 mb-1">Value</h3>
          <textarea
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
            placeholder="Node value"
            rows={5}
            className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1.5 text-xs text-slate-200
                       placeholder-slate-500 focus:outline-none focus:border-blue-500 resize-y"
          />
        </div>
      ) : (
        node.value && (
          <div className="px-4 py-2 border-b border-slate-700">
            <button
              onClick={() => setShowValue(!showValue)}
              className="text-xs text-slate-400 hover:text-slate-200 mb-1"
            >
              {showValue ? '▼ Value' : '▶ Value'} ({node.value.length} chars)
            </button>
            {showValue && (
              <p className="text-xs text-slate-300 whitespace-pre-wrap mt-1 max-h-40 overflow-y-auto">
                {node.value}
              </p>
            )}
          </div>
        )
      )}

      {/* Materialize (code nodes only) */}
      {!editing && isCodeNode(node.nodeType) && (
        <div className="px-4 py-2 border-b border-slate-700">
          <div className="flex items-center gap-2">
            <button
              onClick={handleMaterialize}
              disabled={materializing}
              className="bg-violet-600 hover:bg-violet-500 disabled:bg-slate-700 disabled:text-slate-500
                         text-xs px-2.5 py-1 rounded font-medium transition-colors"
            >
              {materializing ? 'Generating...' : 'Materialize C#'}
            </button>
            {materializedSource && (
              <button
                onClick={() => {
                  navigator.clipboard.writeText(materializedSource);
                }}
                className="bg-slate-700 hover:bg-slate-600 border border-slate-600
                           text-xs px-2 py-1 rounded text-slate-200 transition-colors"
              >
                Copy
              </button>
            )}
          </div>
          {materializedSource && (
            <pre className="mt-2 text-xs text-green-300 bg-slate-900 border border-slate-700 rounded p-2
                            overflow-x-auto max-h-64 overflow-y-auto font-mono whitespace-pre">
              {materializedSource}
            </pre>
          )}
        </div>
      )}

      {/* Attributes */}
      {editing ? (
        <div className="px-4 py-2 border-b border-slate-700">
          <h3 className="text-xs font-medium text-slate-400 mb-2">Attributes</h3>
          <div className="space-y-2">
            {editAttributes.map((attr, index) => (
              <div key={index} className="flex items-center gap-2">
                <input
                  type="text"
                  value={attr.key}
                  onChange={(e) => {
                    const next = [...editAttributes];
                    next[index] = { ...next[index], key: e.target.value };
                    setEditAttributes(next);
                  }}
                  placeholder="key"
                  className="w-40 bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-slate-200
                             placeholder-slate-500 focus:outline-none focus:border-blue-500"
                />
                <input
                  type="text"
                  value={attr.value}
                  onChange={(e) => {
                    const next = [...editAttributes];
                    next[index] = { ...next[index], value: e.target.value };
                    setEditAttributes(next);
                  }}
                  placeholder="value"
                  className="flex-1 bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-slate-200
                             placeholder-slate-500 focus:outline-none focus:border-blue-500"
                />
                <button
                  onClick={() => setEditAttributes((prev) => prev.filter((_, i) => i !== index))}
                  className="bg-slate-700 hover:bg-slate-600 border border-slate-600
                             text-xs px-2 py-1 rounded text-slate-200 transition-colors"
                >
                  Delete
                </button>
              </div>
            ))}
            <button
              onClick={() => setEditAttributes((prev) => [...prev, { key: '', value: '' }])}
              className="bg-slate-700 hover:bg-slate-600 border border-slate-600
                         text-xs px-2 py-1 rounded text-slate-200 transition-colors"
            >
              Add Attribute
            </button>
          </div>
        </div>
      ) : (
        Object.keys(attributes).length > 0 && (
          <div className="px-4 py-2 border-b border-slate-700">
            <h3 className="text-xs font-medium text-slate-400 mb-1">Attributes</h3>
            <div className="space-y-0.5">
              {Object.entries(attributes).map(([key, value]) => (
                <div key={key} className="flex text-xs">
                  <span className="text-slate-500 w-24 flex-shrink-0">{key}</span>
                  <span className="text-slate-300 truncate">{value}</span>
                </div>
              ))}
            </div>
          </div>
        )
      )}

      {/* Graph Edges */}
      {edges.length > 0 && (
        <div className="px-4 py-2 border-b border-slate-700">
          <h3 className="text-xs font-medium text-slate-400 mb-1">
            Connections ({edges.length})
          </h3>
          <div className="space-y-1">
            {edges.map((edge, i) => (
              <button
                key={i}
                onClick={() => edge.targetId && onNavigate(edge.targetId)}
                className="flex items-center gap-2 text-xs w-full text-left hover:bg-slate-700/50 rounded px-1 py-0.5 transition-colors"
              >
                <span className="text-slate-500 w-4 text-center">
                  {EDGE_ICONS[edge.edgeType] || '·'}
                </span>
                <span className="text-cyan-400 font-mono">{edge.edgeType}</span>
                <span className={`text-slate-400 ${edge.direction === 'incoming' ? 'italic' : ''}`}>
                  {edge.direction === 'incoming' ? '←' : '→'}
                </span>
                <span className="text-slate-300 truncate">
                  {edge.targetName || edge.targetId || '?'}
                </span>
                {edge.targetType && (
                  <span className="text-slate-600 text-[10px]">{edge.targetType}</span>
                )}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Children */}
      <div className="px-4 py-2 flex-1">
        <div className="flex items-center justify-between mb-1">
          <h3 className="text-xs font-medium text-slate-400">
            Children ({childCount})
          </h3>
          <button
            onClick={() => setShowCreateChild((v) => !v)}
            className="bg-slate-700 hover:bg-slate-600 border border-slate-600
                       text-xs px-2 py-1 rounded text-slate-200 transition-colors"
          >
            + New
          </button>
        </div>

        {showCreateChild && (
          <div className="mb-2 p-2 bg-slate-700 border border-slate-600 rounded space-y-2">
            <div>
              <label className="block text-[11px] text-slate-300 mb-1">Type</label>
              <select
                value={newChildType}
                onChange={(e) => setNewChildType(e.target.value)}
                className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-slate-200
                           focus:outline-none focus:border-blue-500"
              >
                <optgroup label="Ideation">
                  <option value="idea">idea</option>
                  <option value="question">question</option>
                  <option value="decision">decision</option>
                  <option value="topic">topic</option>
                  <option value="action_item">action_item</option>
                </optgroup>
                <optgroup label="Planning">
                  <option value="plan">plan</option>
                  <option value="plan_phase">plan_phase</option>
                  <option value="plan_step">plan_step</option>
                  <option value="task">task</option>
                  <option value="milestone">milestone</option>
                  <option value="blocker">blocker</option>
                </optgroup>
                <optgroup label="Research">
                  <option value="finding">finding</option>
                </optgroup>
                <optgroup label="Code">
                  <option value="compilation_unit">compilation_unit</option>
                  <option value="namespace">namespace</option>
                  <option value="class">class</option>
                  <option value="struct">struct</option>
                  <option value="method">method</option>
                  <option value="field">field</option>
                  <option value="constructor">constructor</option>
                  <option value="property">property</option>
                  <option value="statement">statement</option>
                </optgroup>
              </select>
            </div>

            <div>
              <label className="block text-[11px] text-slate-300 mb-1">Name</label>
              <input
                type="text"
                value={newChildName}
                onChange={(e) => setNewChildName(e.target.value)}
                className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-slate-200
                           placeholder-slate-500 focus:outline-none focus:border-blue-500"
              />
            </div>

            <div>
              <label className="block text-[11px] text-slate-300 mb-1">Value (optional)</label>
              <textarea
                value={newChildValue}
                onChange={(e) => setNewChildValue(e.target.value)}
                rows={3}
                className="w-full bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-slate-200
                           placeholder-slate-500 focus:outline-none focus:border-blue-500 resize-y"
              />
            </div>

            <div className="flex items-center gap-2">
              <button
                onClick={handleCreateChild}
                disabled={creating}
                className="bg-blue-600 hover:bg-blue-500 disabled:bg-slate-700 disabled:text-slate-500
                           text-xs px-2.5 py-1 rounded font-medium transition-colors"
              >
                {creating ? 'Creating...' : 'Create'}
              </button>
              <button
                onClick={() => {
                  setShowCreateChild(false);
                  setNewChildType('idea');
                  setNewChildName('');
                  setNewChildValue('');
                }}
                disabled={creating}
                className="bg-slate-700 hover:bg-slate-600 border border-slate-600 disabled:opacity-50
                           text-xs px-2.5 py-1 rounded text-slate-200 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {children.length === 0 ? (
          <p className="text-xs text-slate-600 italic">No children</p>
        ) : (
          <div className="space-y-1">
            {children.map((child) => (
              <button
                key={child.id}
                onClick={() => onNavigate(child.id)}
                className="flex items-center gap-2 w-full text-left hover:bg-slate-700/50 rounded px-2 py-1.5 transition-colors group"
              >
                <span className={`${TYPE_COLORS[child.nodeType] || 'bg-gray-600'} text-[10px] px-1.5 py-0.5 rounded-full`}>
                  {child.nodeType}
                </span>
                <span className="text-xs text-slate-300 truncate flex-1 group-hover:text-slate-100">
                  {child.name || child.summary || '(unnamed)'}
                </span>
                {child.status && (
                  <span className={`text-[10px] ${STATUS_COLORS[child.status] || 'text-slate-500'}`}>
                    {child.status}
                  </span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
