// WorkspacePanel — unified node browser with tree nav, detail view, and scoped chat
//
// Left pane: tree navigator (root nodes → drill into children)
// Right pane: node detail + scoped chat input
//
// Depends on: api.ts (getRootNodes, getNodeChildren, scopedChat), NodeDetailPanel
// Used by: App.tsx

import { useState, useEffect, useRef } from 'react';
import {
  type RootNode,
  type NodeChild,
  type ChatMessage,
  getRootNodes,
  getNodeChildren,
  scopedChat,
} from '../api';
import NodeDetailPanel from './NodeDetailPanel';
import { TYPE_COLORS } from '../colors';

// Tree node — either a root node or a child node, unified for display
interface TreeItem {
  id: string;
  nodeType: string;
  name: string | null;
  childCount: number;
  status?: string | null;
  depth: number;
}

export default function WorkspacePanel() {
  // Tree state
  const [roots, setRoots] = useState<RootNode[]>([]);
  const [expanded, setExpanded] = useState<Record<string, NodeChild[]>>({});
  const [treeLoading, setTreeLoading] = useState(true);

  // Selection
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Scoped chat
  const [chatInput, setChatInput] = useState('');
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatStreaming, setChatStreaming] = useState(false);
  const [streamBuffer, setStreamBuffer] = useState('');
  const chatEndRef = useRef<HTMLDivElement>(null);

  // Load root nodes on mount
  useEffect(() => {
    getRootNodes()
      .then(setRoots)
      .catch((err) => console.error('Failed to load roots:', err))
      .finally(() => setTreeLoading(false));
  }, []);

  // Auto-scroll chat
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [chatMessages, streamBuffer]);

  const toggleExpand = async (nodeId: string) => {
    if (expanded[nodeId]) {
      // Collapse
      const next = { ...expanded };
      delete next[nodeId];
      setExpanded(next);
    } else {
      // Expand — fetch children
      try {
        const children = await getNodeChildren(nodeId);
        setExpanded((prev) => ({ ...prev, [nodeId]: children }));
      } catch (err) {
        console.error('Failed to expand:', err);
      }
    }
  };

  const handleSelect = (nodeId: string) => {
    setSelectedId(nodeId);
    setChatMessages([]);
    setStreamBuffer('');
  };

  const handleNavigate = (nodeId: string) => {
    setSelectedId(nodeId);
    setChatMessages([]);
    setStreamBuffer('');
    // Ensure parent chain is expanded — for now just select
  };

  const handleNodeChanged = async () => {
    try {
      const expandedIds = Object.keys(expanded);
      const [nextRoots, expandedChildren] = await Promise.all([
        getRootNodes(),
        Promise.all(
          expandedIds.map(async (id) => ({
            id,
            children: await getNodeChildren(id),
          })),
        ),
      ]);

      const nextExpanded: Record<string, NodeChild[]> = {};
      for (const item of expandedChildren) {
        nextExpanded[item.id] = item.children;
      }

      setRoots(nextRoots);
      setExpanded(nextExpanded);
    } catch (err) {
      console.error('Failed to refresh tree after node change:', err);
    }
  };

  const handleScopedChat = async () => {
    if (!selectedId || !chatInput.trim() || chatStreaming) return;

    const message = chatInput.trim();
    setChatInput('');
    setChatMessages((prev) => [
      ...prev,
      { id: crypto.randomUUID(), speaker: 'user', content: message, createdAt: new Date().toISOString() },
    ]);
    setChatStreaming(true);
    setStreamBuffer('');

    let buffer = '';
    let model = 'assistant';

    await scopedChat(selectedId, message, null, {
      onConversationId: () => {},
      onIntent: () => {},
      onModel: (m) => { model = m.display; },
      onToken: (text) => {
        buffer += text;
        setStreamBuffer(buffer);
      },
      onExtraction: () => {},
      onDebug: () => {},
      onPhase: () => {},
      onToolCall: () => {},
      onToolResult: () => {},
      onPermissionRequest: () => {},
      onDone: () => {
        setChatMessages((prev) => [
          ...prev,
          { id: crypto.randomUUID(), speaker: model, content: buffer, createdAt: new Date().toISOString() },
        ]);
        setStreamBuffer('');
        setChatStreaming(false);
      },
      onError: (err) => {
        setChatMessages((prev) => [
          ...prev,
          { id: crypto.randomUUID(), speaker: 'system:error', content: err, createdAt: new Date().toISOString() },
        ]);
        setChatStreaming(false);
      },
    });
  };

  // Build flat tree items from roots + expanded children
  const buildTreeItems = (): TreeItem[] => {
    const items: TreeItem[] = [];

    const addChildren = (parentId: string, depth: number) => {
      const kids = expanded[parentId];
      if (!kids) return;
      for (const child of kids) {
        items.push({
          id: child.id,
          nodeType: child.nodeType,
          name: child.name || child.summary,
          childCount: 0, // We don't know child count until expanded
          status: child.status,
          depth,
        });
        addChildren(child.id, depth + 1);
      }
    };

    for (const root of roots) {
      items.push({
        id: root.id,
        nodeType: root.nodeType,
        name: root.name,
        childCount: root.childCount,
        depth: 0,
      });
      addChildren(root.id, 1);
    }

    return items;
  };

  const treeItems = buildTreeItems();

  return (
    <div className="flex h-full">
      {/* Left: Tree Navigator */}
      <div className="w-72 border-r border-slate-700 bg-slate-800/50 flex flex-col flex-shrink-0">
        <div className="px-3 py-2 border-b border-slate-700">
          <h2 className="text-sm font-semibold text-slate-300">Node Tree</h2>
        </div>
        <div className="flex-1 overflow-y-auto">
          {treeLoading ? (
            <div className="p-3 text-xs text-slate-500 animate-pulse">Loading...</div>
          ) : treeItems.length === 0 ? (
            <div className="p-3 text-xs text-slate-500">No nodes found</div>
          ) : (
            <div className="py-1">
              {treeItems.map((item) => (
                <div
                  key={item.id}
                  style={{ paddingLeft: `${item.depth * 16 + 8}px` }}
                  className={`flex items-center gap-1.5 py-1 pr-2 cursor-pointer text-xs transition-colors
                    ${selectedId === item.id ? 'bg-slate-700 text-slate-100' : 'text-slate-400 hover:bg-slate-700/50 hover:text-slate-200'}`}
                >
                  {/* Expand toggle */}
                  <button
                    onClick={(e) => { e.stopPropagation(); toggleExpand(item.id); }}
                    className="w-4 h-4 flex items-center justify-center text-slate-500 hover:text-slate-300 flex-shrink-0"
                  >
                    {expanded[item.id] ? '▼' : '▶'}
                  </button>

                  {/* Node info */}
                  <button
                    onClick={() => handleSelect(item.id)}
                    className="flex items-center gap-1.5 flex-1 min-w-0 text-left"
                  >
                    <span className={`${TYPE_COLORS[item.nodeType] || 'bg-gray-600'} text-[9px] px-1 py-0.5 rounded flex-shrink-0`}>
                      {item.nodeType.replace('plan_', '').replace('action_item', 'action')}
                    </span>
                    <span className="truncate">{item.name || '(unnamed)'}</span>
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Right: Detail + Chat */}
      <div className="flex-1 flex flex-col min-w-0">
        {selectedId ? (
          <>
            {/* Detail view — takes available space */}
            <div className="flex-1 overflow-hidden">
              <NodeDetailPanel nodeId={selectedId} onNavigate={handleNavigate} onNodeChanged={handleNodeChanged} />
            </div>

            {/* Scoped chat — bottom section */}
            <div className="border-t border-slate-700 bg-slate-800/30 flex flex-col max-h-[40%]">
              {/* Chat messages */}
              {(chatMessages.length > 0 || streamBuffer) && (
                <div className="flex-1 overflow-y-auto px-3 py-2 space-y-2 min-h-0">
                  {chatMessages.map((msg) => (
                    <div key={msg.id} className="text-xs">
                      <span className={`font-medium ${msg.speaker === 'user' ? 'text-blue-400' : 'text-purple-400'}`}>
                        {msg.speaker}:
                      </span>{' '}
                      <span className="text-slate-300 whitespace-pre-wrap">{msg.content}</span>
                    </div>
                  ))}
                  {streamBuffer && (
                    <div className="text-xs">
                      <span className="font-medium text-purple-400">assistant:</span>{' '}
                      <span className="text-slate-300 whitespace-pre-wrap">{streamBuffer}</span>
                      <span className="animate-pulse text-slate-500">▌</span>
                    </div>
                  )}
                  <div ref={chatEndRef} />
                </div>
              )}

              {/* Input */}
              <div className="px-3 py-2 flex gap-2">
                <input
                  type="text"
                  value={chatInput}
                  onChange={(e) => setChatInput(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && handleScopedChat()}
                  placeholder={`Chat about this ${selectedId ? 'node' : ''}... (@mention for model)`}
                  disabled={chatStreaming}
                  className="flex-1 bg-slate-700 border border-slate-600 rounded px-3 py-1.5 text-sm text-slate-200
                             placeholder-slate-500 focus:outline-none focus:border-blue-500 disabled:opacity-50"
                />
                <button
                  onClick={handleScopedChat}
                  disabled={chatStreaming || !chatInput.trim()}
                  className="bg-blue-600 hover:bg-blue-500 disabled:bg-slate-700 disabled:text-slate-500
                             text-sm px-3 py-1.5 rounded font-medium transition-colors"
                >
                  {chatStreaming ? '...' : 'Send'}
                </button>
              </div>
            </div>
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center text-slate-500">
            <div className="text-center">
              <p className="text-lg">Select a node</p>
              <p className="text-sm mt-1">Click a node in the tree to view details and chat</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
