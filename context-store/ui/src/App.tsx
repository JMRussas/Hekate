// App — top-level layout with view switcher
//
// Views: Chat (three-panel conversation) | Workspace (node browser + scoped chat)
//
// Per-conversation state: each conversation keeps its own messages, threads, stats,
// debug log, and streaming status in a Map. Switching conversations changes which
// state is displayed — active streams continue in the background.
//
// Depends on: api.ts, ChatPanel, ThreadsSidebar, StatsBar, DebugPanel, PlannerPanel, WorkspacePanel, ManagedPlansView
// Used by: main.tsx

import { useState, useEffect, useCallback, useRef } from 'react';
import ChatPanel from './components/ChatPanel';
import ConversationList from './components/ConversationList';
import ThreadsSidebar from './components/ThreadsSidebar';
import StatsBar from './components/StatsBar';
import DebugPanel from './components/DebugPanel';
import PlannerPanel from './components/PlannerPanel';
import ManagedPlansView from './components/ManagedPlansView';
import WorkspacePanel from './components/WorkspacePanel';
import {
  type ChatMessage,
  type ThreadItem,
  type Stats,
  type ExtractedItem,
  type DebugData,
  type PreviewResult,
  type PermissionConfig,
  PERMISSION_LEVELS,
  getConversation,
  getConversationDebug,
  getThreads,
  getStats,
  getPermissions,
  setPermission,
  subscribeToEvents,
} from './api';

type RightTab = 'stats' | 'debug' | 'planner';
type AppView = 'chat' | 'workspace' | 'plans';

// Per-conversation cached state — streams write here even when another conversation is displayed
interface ConversationState {
  messages: ChatMessage[];
  threads: ThreadItem[];
  stats: Stats | null;
  debugLog: Record<number, DebugData>;
  currentIntent: string | null;
  intentConfidence: string | null;
  currentModel: string | null;
  hasStreaming: boolean; // true while any SSE stream is active
  permissions: PermissionConfig | null;
}

function useResizable(initialWidth: number, minWidth: number, maxWidth: number, side: 'left' | 'right') {
  const [width, setWidth] = useState(initialWidth);
  const dragging = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(0);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    dragging.current = true;
    startX.current = e.clientX;
    startWidth.current = width;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMouseMove = (ev: MouseEvent) => {
      if (!dragging.current) return;
      const delta = side === 'left'
        ? ev.clientX - startX.current
        : startX.current - ev.clientX;
      setWidth(Math.min(maxWidth, Math.max(minWidth, startWidth.current + delta)));
    };
    const onMouseUp = () => {
      dragging.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
    };
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  }, [width, side, minWidth, maxWidth]);

  return { width, onMouseDown };
}

const EMPTY_CONV_STATE: ConversationState = {
  messages: [],
  threads: [],
  stats: null,
  debugLog: {},
  currentIntent: null,
  intentConfidence: null,
  currentModel: null,
  hasStreaming: false,
  permissions: null,
};

export default function App() {
  const [appView, setAppView] = useState<AppView>('chat');
  const [conversationId, setConversationId] = useState<string | null>(
    () => localStorage.getItem('conversationId'),
  );

  // Per-conversation state cache — keyed by conversation ID (null = new conversation)
  const convCache = useRef<Map<string | null, ConversationState>>(new Map());

  // Force re-render when cache contents change (cache is a ref for stable identity in callbacks)
  const [, forceUpdate] = useState(0);
  const rerender = useCallback(() => forceUpdate((n) => n + 1), []);

  // Get or create state for a conversation
  const getConvState = useCallback((id: string | null): ConversationState => {
    let state = convCache.current.get(id);
    if (!state) {
      state = { ...EMPTY_CONV_STATE };
      convCache.current.set(id, state);
    }
    return state;
  }, []);

  // Update a field in a conversation's state and re-render if it's the active conversation
  const updateConvState = useCallback((
    id: string | null,
    updater: (prev: ConversationState) => Partial<ConversationState>,
  ) => {
    const state = getConvState(id);
    const updates = updater(state);
    Object.assign(state, updates);
    // Always re-render — the active conversation might have changed, and
    // we also want the conversation list to show streaming indicators
    rerender();
  }, [getConvState, rerender]);

  // Convenience: current conversation's state
  const current = getConvState(conversationId);

  // Panel sizing
  const leftPanel = useResizable(256, 180, 500, 'left');
  const rightPanel = useResizable(320, 220, 600, 'right');

  // Debug/preview state (not per-conversation — preview is for the input box)
  const [rightTab, setRightTab] = useState<RightTab>('debug');
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  // Load conversation on mount
  useEffect(() => {
    if (conversationId) {
      loadConversation(conversationId);
    }
  }, []);

  // Subscribe to system events via SSE — append to active conversation
  useEffect(() => {
    const es = subscribeToEvents((msg) => {
      // Use a ref-read of conversationId so this doesn't need to re-subscribe
      const targetId = conversationId;
      updateConvState(targetId, (prev) => ({
        messages: [
          ...prev.messages,
          {
            id: crypto.randomUUID(),
            speaker: `system:${msg.level}`,
            content: msg.text,
            createdAt: new Date().toISOString(),
          },
        ],
      }));
    });
    return () => es.close();
  }, []);

  const loadConversation = async (id: string) => {
    // Load independently — don't let one failure kill the others
    getConversation(id)
      .then((conv) => updateConvState(id, () => ({ messages: conv.turns })))
      .catch(() => updateConvState(id, () => ({ messages: [] })));

    getThreads(id)
      .then((data) => updateConvState(id, () => ({ threads: data.items })))
      .catch(() => updateConvState(id, () => ({ threads: [] })));

    getStats(id)
      .then((data) => updateConvState(id, () => ({ stats: data })))
      .catch(() => updateConvState(id, () => ({ stats: null })));

    getConversationDebug(id)
      .then((data) => {
        if (data) updateConvState(id, () => ({ debugLog: { 0: data } }));
      })
      .catch(() => {});

    getPermissions(id)
      .then((data) => updateConvState(id, () => ({ permissions: data })))
      .catch(() => {});
  };

  // When a stream creates a new conversation, migrate the null-keyed state to the real ID.
  // Returns the new ID so the caller can update its captured targetId.
  const handleConversationId = useCallback((id: string) => {
    const nullState = convCache.current.get(null);
    if (nullState) {
      convCache.current.delete(null);
      convCache.current.set(id, nullState);
    }
    setConversationId(id);
    localStorage.setItem('conversationId', id);
  }, []);

  // All callbacks accept a targetId so each stream writes to the conversation it was
  // started on — not whichever conversation is currently displayed.
  const handleNewMessage = useCallback((targetId: string | null, msg: ChatMessage) => {
    updateConvState(targetId, (prev) => ({
      messages: [...prev.messages, msg],
    }));
  }, [updateConvState]);

  const handleUpdateMessage = useCallback((targetId: string | null, messageId: string, updates: Partial<ChatMessage>) => {
    updateConvState(targetId, (prev) => ({
      messages: prev.messages.map(m => m.id === messageId ? { ...m, ...updates } : m),
    }));
  }, [updateConvState]);

  const handleExtraction = useCallback((targetId: string | null, items: ExtractedItem[]) => {
    if (items.length > 0 && targetId) {
      getThreads(targetId).then((data) =>
        updateConvState(targetId, () => ({ threads: data.items })),
      );
      getStats(targetId).then((data) =>
        updateConvState(targetId, () => ({ stats: data })),
      );
    }
  }, [updateConvState]);

  const handleIntentUpdate = useCallback((targetId: string | null, intent: string, confidence: string) => {
    updateConvState(targetId, () => ({
      currentIntent: intent,
      intentConfidence: confidence,
    }));
    if (targetId) {
      getStats(targetId).then((data) =>
        updateConvState(targetId, () => ({ stats: data })),
      );
    }
  }, [updateConvState]);

  const handleModelUpdate = useCallback((targetId: string | null, model: string) => {
    updateConvState(targetId, () => ({ currentModel: model }));
  }, [updateConvState]);

  const handleDebugData = useCallback((targetId: string | null, messageIndex: number, data: DebugData) => {
    updateConvState(targetId, (prev) => ({
      debugLog: { ...prev.debugLog, [messageIndex]: data },
    }));
  }, [updateConvState]);

  const handleStreamingChange = useCallback((targetId: string | null, streaming: boolean) => {
    updateConvState(targetId, () => ({ hasStreaming: streaming }));
  }, [updateConvState]);

  const refreshThreads = () => {
    if (conversationId) {
      getThreads(conversationId).then((data) =>
        updateConvState(conversationId, () => ({ threads: data.items })),
      );
      getStats(conversationId).then((data) =>
        updateConvState(conversationId, () => ({ stats: data })),
      );
    }
  };

  const handleSelectConversation = async (id: string) => {
    setConversationId(id);
    localStorage.setItem('conversationId', id);
    setPreview(null);
    // Only load from API if we don't already have cached data
    const cached = convCache.current.get(id);
    if (!cached || cached.messages.length === 0) {
      await loadConversation(id);
    }
  };

  const handleNewConversation = () => {
    localStorage.removeItem('conversationId');
    setConversationId(null);
    setPreview(null);
    // Don't clear cache — other conversations keep their state
  };

  // Check if any conversation has active streams (for conversation list indicators)
  const hasStreamingConversation = useCallback((id: string | null): boolean => {
    return convCache.current.get(id)?.hasStreaming ?? false;
  }, []);

  const handlePermissionChange = async (level: number) => {
    if (!conversationId) return;
    const config = await setPermission(conversationId, level);
    updateConvState(conversationId, () => ({ permissions: config }));
  };

  return (
    <div className="h-screen flex flex-col bg-slate-900">
      {/* Header */}
      <header className="bg-slate-800 border-b border-slate-700 px-4 py-2 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <h1 className="text-lg font-semibold text-slate-200">Ideation Assistant</h1>
          {/* View switcher */}
          <div className="flex bg-slate-700/50 rounded-lg p-0.5">
            <button
              onClick={() => setAppView('chat')}
              className={`px-3 py-1 text-xs font-medium rounded-md transition-colors ${
                appView === 'chat'
                  ? 'bg-slate-600 text-slate-100'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              Chat
            </button>
            <button
              onClick={() => setAppView('workspace')}
              className={`px-3 py-1 text-xs font-medium rounded-md transition-colors ${
                appView === 'workspace'
                  ? 'bg-slate-600 text-slate-100'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              Workspace
            </button>
            <button
              onClick={() => setAppView('plans')}
              className={`px-3 py-1 text-xs font-medium rounded-md transition-colors ${
                appView === 'plans'
                  ? 'bg-slate-600 text-slate-100'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              Plans
            </button>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {/* Permission level selector */}
          {appView === 'chat' && conversationId && (
            <div className="flex bg-slate-700/50 rounded-lg p-0.5">
              {PERMISSION_LEVELS.map((label, i) => {
                const activeLevel = current.permissions?.defaultLevel ?? 2;
                const colors = ['text-red-400', 'text-amber-400', 'text-blue-400', 'text-green-400'];
                const activeBg = ['bg-red-600/30', 'bg-amber-600/30', 'bg-blue-600/30', 'bg-green-600/30'];
                return (
                  <button
                    key={label}
                    onClick={() => handlePermissionChange(i)}
                    className={`px-2 py-0.5 text-[10px] font-medium rounded transition-colors ${
                      i === activeLevel
                        ? `${activeBg[i]} ${colors[i]}`
                        : 'text-slate-500 hover:text-slate-300'
                    }`}
                    title={`Set permission to ${label}`}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
          )}
          {appView === 'chat' && (
            <button
              onClick={handleNewConversation}
              className="text-sm text-slate-400 hover:text-slate-200 border border-slate-600
                         hover:border-slate-500 px-3 py-1 rounded transition-colors"
            >
              New Conversation
            </button>
          )}
        </div>
      </header>

      {/* View content */}
      <div className="flex-1 flex overflow-hidden">
        {appView === 'plans' ? (
          <ManagedPlansView />
        ) : appView === 'workspace' ? (
          <WorkspacePanel />
        ) : (
          <>
            {/* Left: Conversations + Threads (split vertically) */}
            <aside className="border-r border-slate-700 bg-slate-800/50 flex-shrink-0 relative flex flex-col"
              style={{ width: leftPanel.width }}>
              {/* Top: Conversation picker */}
              <div className="flex-shrink-0 max-h-[40%] overflow-y-auto border-b border-slate-700 relative z-20">
                <ConversationList
                  activeId={conversationId}
                  onSelect={handleSelectConversation}
                  isStreaming={hasStreamingConversation}
                />
              </div>
              {/* Bottom: Threads */}
              <div className="flex-1 overflow-y-auto min-h-0">
                <ThreadsSidebar items={current.threads} onRefresh={refreshThreads} />
              </div>
              {/* Drag handle */}
              <div
                onMouseDown={leftPanel.onMouseDown}
                className="absolute top-0 right-0 w-1.5 h-full cursor-col-resize hover:bg-blue-500/30 active:bg-blue-500/50 transition-colors z-10"
              />
            </aside>

            {/* Center: Chat */}
            <main className="flex-1 flex flex-col min-w-0">
              <ChatPanel
                conversationId={conversationId}
                messages={current.messages}
                onConversationId={handleConversationId}
                onNewMessage={handleNewMessage}
                onUpdateMessage={handleUpdateMessage}
                onExtraction={handleExtraction}
                onIntentUpdate={handleIntentUpdate}
                onModelUpdate={handleModelUpdate}
                onDebugData={handleDebugData}
                onStreamingChange={handleStreamingChange}
                onPreview={setPreview}
                onPreviewLoading={setPreviewLoading}
                debugOpen={rightTab === 'debug'}
              />
            </main>

            {/* Right: Stats / Debug (tabbed) */}
            <aside className="border-l border-slate-700 bg-slate-800/50 flex-shrink-0 flex flex-col relative"
              style={{ width: rightPanel.width }}>
              {/* Drag handle */}
              <div
                onMouseDown={rightPanel.onMouseDown}
                className="absolute top-0 left-0 w-1.5 h-full cursor-col-resize hover:bg-cyan-500/30 active:bg-cyan-500/50 transition-colors z-10"
              />
              {/* Tab bar */}
              <div className="flex border-b border-slate-700">
                <button
                  onClick={() => setRightTab('stats')}
                  className={`flex-1 px-3 py-1.5 text-xs font-medium transition-colors ${
                    rightTab === 'stats'
                      ? 'text-slate-200 border-b-2 border-blue-500'
                      : 'text-slate-500 hover:text-slate-300'
                  }`}
                >
                  Context
                </button>
                <button
                  onClick={() => setRightTab('debug')}
                  className={`flex-1 px-3 py-1.5 text-xs font-medium transition-colors ${
                    rightTab === 'debug'
                      ? 'text-slate-200 border-b-2 border-cyan-500'
                      : 'text-slate-500 hover:text-slate-300'
                  }`}
                >
                  Debug
                </button>
                <button
                  onClick={() => setRightTab('planner')}
                  className={`flex-1 px-3 py-1.5 text-xs font-medium transition-colors ${
                    rightTab === 'planner'
                      ? 'text-slate-200 border-b-2 border-green-500'
                      : 'text-slate-500 hover:text-slate-300'
                  }`}
                >
                  Planner
                </button>
              </div>

              {/* Tab content */}
              {rightTab === 'stats' && (
                <StatsBar
                  stats={current.stats}
                  currentIntent={current.currentIntent}
                  intentConfidence={current.intentConfidence}
                  currentModel={current.currentModel}
                  permissions={current.permissions}
                />
              )}
              {rightTab === 'debug' && (
                <DebugPanel
                  debugLog={current.debugLog}
                  preview={preview}
                  previewLoading={previewLoading}
                />
              )}
              {rightTab === 'planner' && (
                <PlannerPanel />
              )}
            </aside>
          </>
        )}
      </div>
    </div>
  );
}
