// StatsBar — shows intent classification, session stats, and permission config
//
// Displays current intent, total ideas, open questions, parked count,
// and the active permission level with any per-model overrides.
// Depends on: api.ts types
// Used by: App.tsx

import type { Stats, PermissionConfig } from '../api';
import { PERMISSION_LEVELS } from '../api';
import { MODEL_COLORS, PERMISSION_COLORS } from '../colors';

interface Props {
  stats: Stats | null;
  currentIntent: string | null;
  intentConfidence: string | null;
  currentModel: string | null;
  permissions: PermissionConfig | null;
}

export default function StatsBar({ stats, currentIntent, intentConfidence, currentModel, permissions }: Props) {
  return (
    <div className="h-full overflow-y-auto p-3">
      <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">Context</h2>

      {/* Permission level */}
      {permissions && (
        <div className="mb-4">
          <div className="text-xs text-slate-500 mb-1">Permission Level</div>
          <div className="flex items-center gap-2">
            <span className={`text-sm px-2 py-0.5 rounded border ${
              PERMISSION_COLORS[permissions.defaultLevel] ?? PERMISSION_COLORS[2]
            }`}>
              {PERMISSION_LEVELS[permissions.defaultLevel] ?? 'Unknown'}
            </span>
          </div>
          {Object.keys(permissions.modelOverrides).length > 0 && (
            <div className="mt-2 space-y-1">
              <div className="text-[10px] text-slate-600 uppercase">Model Overrides</div>
              {Object.entries(permissions.modelOverrides).map(([model, level]) => (
                <div key={model} className="flex items-center gap-2">
                  <span className="text-xs text-slate-400">{model}</span>
                  <span className="text-[10px] text-slate-500">
                    {PERMISSION_LEVELS[level] ?? level}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Current model */}
      {currentModel && (
        <div className="mb-4">
          <div className="text-xs text-slate-500 mb-1">Model</div>
          <span className={`border text-sm px-2 py-0.5 rounded ${MODEL_COLORS[currentModel] || MODEL_COLORS.claude}`}>
            {currentModel}
          </span>
        </div>
      )}

      {/* Current intent */}
      {currentIntent && (
        <div className="mb-4">
          <div className="text-xs text-slate-500 mb-1">Last Intent</div>
          <div className="flex items-center gap-2">
            <span className="bg-indigo-600/20 border border-indigo-500/30 text-indigo-300 text-sm px-2 py-0.5 rounded">
              {currentIntent}
            </span>
            {intentConfidence && (
              <span className="text-xs text-slate-500">{intentConfidence}</span>
            )}
          </div>
        </div>
      )}

      {/* Stats grid */}
      {stats && (
        <div className="grid grid-cols-2 gap-2">
          <StatCard label="Turns" value={stats.turnCount} color="blue" />
          <StatCard label="Ideas" value={stats.ideaCount} color="yellow" />
          <StatCard label="Questions" value={stats.questionCount} color="purple" />
          <StatCard label="Parked" value={stats.parkedCount} color="slate" />
        </div>
      )}

      {!stats && !currentIntent && !permissions && (
        <p className="text-sm text-slate-500 italic">Send a message to see stats</p>
      )}
    </div>
  );
}

function StatCard({ label, value, color }: { label: string; value: number; color: string }) {
  const colorMap: Record<string, string> = {
    blue: 'border-blue-500/30 text-blue-300',
    yellow: 'border-yellow-500/30 text-yellow-300',
    purple: 'border-purple-500/30 text-purple-300',
    slate: 'border-slate-500/30 text-slate-300',
  };

  return (
    <div className={`border rounded-lg p-2 ${colorMap[color] || colorMap.slate}`}>
      <div className="text-2xl font-bold">{value}</div>
      <div className="text-xs text-slate-500">{label}</div>
    </div>
  );
}
