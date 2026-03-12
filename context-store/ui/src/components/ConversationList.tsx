// ConversationList — shows previous conversations for selection
//
// Fetches conversation list from API, displays as clickable cards.
// Active conversation is highlighted. Refreshes on mount and when
// a new conversation is created.
//
// Depends on: api.ts
// Used by: App.tsx

import { useState, useEffect } from 'react';
import { type Conversation, listConversations } from '../api';

interface Props {
  activeId: string | null;
  onSelect: (id: string) => void;
  isStreaming?: (id: string | null) => boolean;
}

export default function ConversationList({ activeId, onSelect, isStreaming }: Props) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    try {
      const data = await listConversations();
      setConversations(data);
    } catch {
      setConversations([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  // Refresh when activeId changes (new conversation was created)
  useEffect(() => { load(); }, [activeId]);

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    if (diffMins < 1) return 'just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    if (diffDays < 7) return `${diffDays}d ago`;
    return d.toLocaleDateString();
  };

  return (
    <div className="p-3 overflow-y-auto">
      <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-2">
        Conversations
      </h2>

      {loading && (
        <p className="text-xs text-slate-500 animate-pulse">Loading...</p>
      )}

      {!loading && conversations.length === 0 && (
        <p className="text-xs text-slate-500 italic">No conversations yet</p>
      )}

      <div className="space-y-1">
        {conversations.map((conv) => (
          <button
            key={conv.id}
            onClick={() => onSelect(conv.id)}
            className={`w-full text-left rounded-lg px-2 py-1.5 transition-colors ${
              conv.id === activeId
                ? 'bg-blue-600/20 border border-blue-500/30'
                : 'hover:bg-slate-700/50 border border-transparent'
            }`}
          >
            <div className="flex items-center justify-between gap-1">
              <span className={`text-xs font-medium truncate ${
                conv.id === activeId ? 'text-blue-300' : 'text-slate-300'
              }`}>
                {conv.name}
              </span>
              <span className="text-[10px] text-slate-600 flex-shrink-0 flex items-center gap-1">
                {isStreaming?.(conv.id) && (
                  <span className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-pulse" title="Streaming" />
                )}
                {conv.turnCount}
              </span>
            </div>
            <div className="text-[10px] text-slate-600 mt-0.5">
              {formatDate(conv.createdAt)}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
