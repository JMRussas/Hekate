// Shared color constants for node types, statuses, models, and permissions
//
// Single source of truth for all UI color mappings.
//
// Depends on: (none)
// Used by:    NodeDetailPanel, WorkspacePanel, ChatPanel, StatsBar, PlannerPanel

export const TYPE_COLORS: Record<string, string> = {
  // Ideation domain
  conversation: 'bg-blue-600',
  thread: 'bg-blue-500',
  turn: 'bg-slate-500',
  topic: 'bg-indigo-600',
  idea: 'bg-yellow-600',
  question: 'bg-orange-600',
  decision: 'bg-green-600',
  action_item: 'bg-red-600',
  interpretation: 'bg-indigo-500',
  // Planning domain
  plan: 'bg-purple-600',
  plan_phase: 'bg-purple-500',
  plan_step: 'bg-purple-400',
  task: 'bg-teal-600',
  risk: 'bg-red-500',
  blocker: 'bg-red-500',
  milestone: 'bg-cyan-600',
  test_spec: 'bg-emerald-600',
  retrospective: 'bg-amber-600',
  revision: 'bg-amber-500',
  // Research domain
  roadmap: 'bg-cyan-700',
  research: 'bg-cyan-600',
  finding: 'bg-cyan-700',
  open_question: 'bg-orange-500',
  reference: 'bg-slate-600',
  // Code domain
  compilation_unit: 'bg-violet-600',
  using_directive: 'bg-slate-600',
  namespace: 'bg-violet-500',
  class: 'bg-sky-600',
  struct: 'bg-sky-500',
  field: 'bg-lime-600',
  constructor: 'bg-amber-600',
  method: 'bg-emerald-600',
  parameter: 'bg-slate-500',
  block: 'bg-slate-600',
  statement: 'bg-slate-500',
  property: 'bg-lime-500',
  enum: 'bg-orange-500',
  // Tool-use domain
  tool_call: 'bg-teal-500',
  tool_result: 'bg-teal-400',
  skill: 'bg-indigo-600',
  pending_action: 'bg-amber-500',
};

export const STATUS_COLORS: Record<string, string> = {
  completed: 'text-green-400',
  in_progress: 'text-yellow-400',
  pending: 'text-slate-400',
  mentioned: 'text-blue-400',
  explored: 'text-cyan-400',
  parked: 'text-orange-400',
  resolved: 'text-green-400',
  committed: 'text-green-400',
  blocking: 'text-red-400',
  proposed: 'text-amber-400',
};

export const MODEL_COLORS: Record<string, string> = {
  sonnet: 'bg-purple-600/20 border-purple-500/30 text-purple-300',
  opus: 'bg-purple-800/20 border-purple-700/30 text-purple-200',
  haiku: 'bg-purple-400/20 border-purple-300/30 text-purple-200',
  claude: 'bg-purple-600/20 border-purple-500/30 text-purple-300',
  gemini: 'bg-blue-600/20 border-blue-500/30 text-blue-300',
  flash: 'bg-blue-500/20 border-blue-400/30 text-blue-300',
  pro: 'bg-blue-700/20 border-blue-600/30 text-blue-300',
  codex: 'bg-green-600/20 border-green-500/30 text-green-300',
  gpt: 'bg-green-600/20 border-green-500/30 text-green-300',
  ollama: 'bg-orange-600/20 border-orange-500/30 text-orange-300',
  qwen: 'bg-orange-600/20 border-orange-500/30 text-orange-300',
};

export const MODEL_BORDER_COLORS: Record<string, string> = {
  sonnet: 'border-purple-600/40',
  opus: 'border-purple-800/40',
  haiku: 'border-purple-400/40',
  claude: 'border-purple-600/40',
  gemini: 'border-blue-600/40',
  flash: 'border-blue-500/40',
  pro: 'border-blue-700/40',
  codex: 'border-green-600/40',
  gpt: 'border-green-600/40',
  ollama: 'border-orange-600/40',
  qwen: 'border-orange-600/40',
};

export const PERMISSION_COLORS: Record<number, string> = {
  0: 'border-red-500/30 text-red-300',
  1: 'border-amber-500/30 text-amber-300',
  2: 'border-blue-500/30 text-blue-300',
  3: 'border-green-500/30 text-green-300',
};

export const EDGE_ICONS: Record<string, string> = {
  EXTRACTED: '↗',
  SPAWNED_FROM: '↰',
  RELATES_TO: '↔',
  CONTRADICTS: '⊘',
  BLOCKS: '⛔',
  CONSTRAINS: '⚠',
  IMPLEMENTED_BY: '⚙',
  PRODUCES: '→',
  CALLS: '→',
  REFERENCES: '→',
  DEPENDS_ON: '←',
  MODIFIES: '✎',
  INFORMS: '→',
  PRODUCED: '→',
  TRIGGERED: '→',
  FORKED_FROM: '↰',
  OBSERVED: '→',
  INFORMED: '→',
};
