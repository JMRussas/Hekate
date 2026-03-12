// ThreadsSidebar — lists ideas grouped by status
//
// Shows ideas, questions, decisions extracted from conversation.
// Park/resume buttons for lifecycle management.
// Depends on: api.ts
// Used by: App.tsx

import { type ThreadItem, parkIdea, resumeIdea } from '../api';

interface Props {
  items: ThreadItem[];
  onRefresh: () => void;
}

const statusColors: Record<string, string> = {
  mentioned: 'border-yellow-500/30 bg-yellow-500/5',
  explored: 'border-blue-500/30 bg-blue-500/5',
  parked: 'border-slate-500/30 bg-slate-500/5',
  committed: 'border-green-500/30 bg-green-500/5',
};

const typeIcons: Record<string, string> = {
  idea: '💡',
  question: '❓',
  decision: '✅',
  action_item: '📋',
};

export default function ThreadsSidebar({ items, onRefresh }: Props) {
  const grouped = {
    active: items.filter((i) => i.status !== 'parked'),
    parked: items.filter((i) => i.status === 'parked'),
  };

  const handlePark = async (id: string) => {
    await parkIdea(id);
    onRefresh();
  };

  const handleResume = async (id: string) => {
    await resumeIdea(id);
    onRefresh();
  };

  return (
    <div className="h-full overflow-y-auto p-3">
      <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Threads</h2>

      {items.length === 0 && (
        <p className="text-sm text-slate-500 italic">No ideas extracted yet</p>
      )}

      {/* Active items */}
      {grouped.active.length > 0 && (
        <div className="space-y-2 mb-4">
          {grouped.active.map((item) => (
            <div
              key={item.id}
              className={`border rounded-lg p-2 ${statusColors[item.status] || statusColors.mentioned}`}
            >
              <div className="flex items-start justify-between gap-1">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1">
                    <span className="text-sm">{typeIcons[item.nodeType] || '📌'}</span>
                    <span className="text-sm font-medium text-slate-200 truncate">
                      {item.name || 'Untitled'}
                    </span>
                  </div>
                  {item.value && (
                    <p className="text-xs text-slate-400 mt-1 line-clamp-2">{item.value}</p>
                  )}
                </div>
                <button
                  onClick={() => handlePark(item.id)}
                  className="text-xs text-slate-500 hover:text-yellow-400 flex-shrink-0"
                  title="Park this idea"
                >
                  park
                </button>
              </div>
              <div className="mt-1">
                <span className="text-[10px] text-slate-500 uppercase">{item.status}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Parked items */}
      {grouped.parked.length > 0 && (
        <>
          <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2 mt-4">
            Parked ({grouped.parked.length})
          </h3>
          <div className="space-y-2">
            {grouped.parked.map((item) => (
              <div
                key={item.id}
                className="border border-slate-700/50 rounded-lg p-2 opacity-60 hover:opacity-100 transition-opacity"
              >
                <div className="flex items-center justify-between gap-1">
                  <span className="text-sm text-slate-400 truncate">
                    {typeIcons[item.nodeType] || '📌'} {item.name || 'Untitled'}
                  </span>
                  <button
                    onClick={() => handleResume(item.id)}
                    className="text-xs text-slate-500 hover:text-green-400 flex-shrink-0"
                    title="Resume this idea"
                  >
                    resume
                  </button>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
