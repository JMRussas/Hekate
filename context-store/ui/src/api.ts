// API helpers and SSE streaming for the ideation assistant

const BASE = '/api';

export interface ChatMessage {
  id: string;
  speaker: string;
  content: string;
  createdAt: string;
  isPlaceholder?: boolean;
}

export interface Conversation {
  id: string;
  name: string;
  createdAt: string;
  turnCount: number;
}

export interface ThreadItem {
  id: string;
  nodeType: string;
  name: string | null;
  value: string | null;
  status: string;
}

export interface Stats {
  conversationId: string;
  turnCount: number;
  ideaCount: number;
  questionCount: number;
  parkedCount: number;
}

export interface ExtractedItem {
  type: string;
  name: string;
  description: string;
}

export interface ModelInfo {
  provider: string;
  model: string;
  display: string;
}

// Interpreter output — the Interpreter role's structured understanding
export interface InterpretData {
  intent: string;
  confidence: number;
  reasoning: string | null;
  project: { type: string; name: string | null; id: string | null };
  entities: { mention: string; nodeId: string | null; nodeName: string | null; nodeType: string | null; score: number | null }[];
  isRegexFallback: boolean;
  durationMs: number;
}

// Debug data accumulated per message during streaming
export interface DebugData {
  parse?: { originalMessage: string; mention: string; cleanedMessage: string; model: ModelInfo; durationMs: number };
  interpret?: InterpretData;
  intent?: { intent: string; confidence: string; pattern: string | null };
  context?: { nodeCount: number; nodes: { nodeType: string; name: string | null; score: number | null }[]; coverage?: { Embedded: number; Total: number; Pct: number } | null; durationMs: number };
  prompt?: { systemPromptLength: number; userPromptLength: number; tokenEstimate: number; naiveTokenEstimate?: number };
  timing?: { parseMs: number; classifyMs: number; contextMs: number; generateMs: number; extractMs: number; totalMs: number };
}

// Preview result from dry-run endpoint
export interface PreviewResult {
  parse: { originalMessage: string; mention: string; cleanedMessage: string; model: ModelInfo };
  intent: { intent: string; confidence: string; pattern: string | null };
  context: { nodeCount: number; nodes: { nodeType: string; name: string | null; score: number | null }[]; tokenEstimate: number };
  promptPreview: string;
}

export interface PermissionRequestEvent {
  actionId: string;
  model: string;
  skill: string;
  description: string;
  paramsJson: string;
}

export interface StreamCallbacks {
  onConversationId: (id: string) => void;
  onIntent: (data: { intent: string; confidence: string; pattern: string | null }) => void;
  onModel: (data: ModelInfo) => void;
  onToken: (text: string) => void;
  onExtraction: (data: { items: ExtractedItem[]; count: number }) => void;
  onDebug: (data: DebugData) => void;
  onPhase: (phase: string) => void;
  onToolCall: (data: { name: string; toolId: string }) => void;
  onToolResult: (data: { name: string; toolId: string; resultLength: number }) => void;
  onPermissionRequest: (data: PermissionRequestEvent) => void;
  onDone: () => void;
  onError: (error: string) => void;
}

export async function streamChat(
  message: string,
  conversationId: string | null,
  callbacks: StreamCallbacks,
  url: string = `${BASE}/chat`,
): Promise<void> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, conversationId }),
  });

  if (!response.ok) {
    callbacks.onError(`HTTP ${response.status}`);
    return;
  }

  const reader = response.body?.getReader();
  if (!reader) {
    callbacks.onError('No response body');
    return;
  }

  const decoder = new TextDecoder();
  let buffer = '';
  let eventType = '';  // Persists across chunks so split event/data pairs work
  let doneFired = false;

  const processLine = (line: string) => {
    if (line.startsWith('event: ')) {
      eventType = line.slice(7).trim();
    } else if (line.startsWith('data: ') && eventType) {
      const data = JSON.parse(line.slice(6));
      switch (eventType) {
        case 'conversation_id':
          callbacks.onConversationId(data.id);
          break;
        case 'intent':
          callbacks.onIntent(data);
          break;
        case 'model':
          callbacks.onModel(data);
          break;
        case 'token':
          callbacks.onToken(data.text);
          break;
        case 'extraction':
          callbacks.onExtraction(data);
          break;
        case 'debug_parse':
          callbacks.onDebug({ parse: data });
          break;
        case 'debug_interpret':
          callbacks.onDebug({ interpret: data });
          break;
        case 'debug_context':
          callbacks.onDebug({ context: data });
          break;
        case 'debug_prompt':
          callbacks.onDebug({ prompt: data });
          break;
        case 'debug_timing':
          callbacks.onDebug({ timing: data });
          break;
        case 'phase':
          callbacks.onPhase(data.phase);
          break;
        case 'tool_call':
          callbacks.onToolCall(data);
          break;
        case 'tool_result':
          callbacks.onToolResult(data);
          break;
        case 'permission_request':
          callbacks.onPermissionRequest(data);
          break;
        case 'done':
          doneFired = true;
          callbacks.onDone();
          break;
      }
      eventType = '';
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      processLine(line);
    }
  }

  // Drain remaining buffer (last chunk may not end with \n)
  if (buffer.trim()) {
    for (const line of buffer.split('\n')) {
      processLine(line);
    }
  }

  // Safety net: if stream closed without done event, still signal completion
  if (!doneFired) {
    callbacks.onDone();
  }
}

export async function listConversations(): Promise<Conversation[]> {
  const res = await fetch(`${BASE}/conversations`);
  if (!res.ok) throw new Error(`Failed to list conversations: ${res.status}`);
  return res.json();
}

export async function getConversation(id: string): Promise<{ id: string; name: string; turns: ChatMessage[] }> {
  const res = await fetch(`${BASE}/conversation/${id}`);
  if (!res.ok) throw new Error(`Failed to get conversation: ${res.status}`);
  return res.json();
}

export async function getThreads(conversationId: string): Promise<{ items: ThreadItem[] }> {
  const res = await fetch(`${BASE}/threads/${conversationId}`);
  if (!res.ok) throw new Error(`Failed to get threads: ${res.status}`);
  return res.json();
}

export async function getStats(conversationId: string): Promise<Stats> {
  const res = await fetch(`${BASE}/stats/${conversationId}`);
  if (!res.ok) throw new Error(`Failed to get stats: ${res.status}`);
  return res.json();
}

export async function getModels(): Promise<{ models: { mention: string; provider: string; model: string; display: string }[] }> {
  const res = await fetch(`${BASE}/models`);
  if (!res.ok) throw new Error(`Failed to get models: ${res.status}`);
  return res.json();
}

export async function previewMessage(message: string, conversationId: string | null): Promise<PreviewResult> {
  const res = await fetch(`${BASE}/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, conversationId }),
  });
  if (!res.ok) throw new Error(`Failed to preview: ${res.status}`);
  return res.json();
}

export async function getConversationDebug(conversationId: string): Promise<DebugData | null> {
  const res = await fetch(`${BASE}/conversation/${conversationId}/debug`);
  if (!res.ok) return null;
  const data = await res.json();
  return data;
}

export async function parkIdea(nodeId: string): Promise<void> {
  await fetch(`${BASE}/command/park`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ nodeId }),
  });
}

export async function resumeIdea(nodeId: string): Promise<void> {
  await fetch(`${BASE}/command/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ nodeId }),
  });
}

// --- Plan types ---

export interface PlanSummary {
  id: string;
  name: string;
  value: string | null;
  planType: string | null;
  status: string | null;
  priority: string | null;
  targetDate: string | null;
  phaseCount: number;
  createdAt: string;
}

export interface PlanNode {
  id: string;
  nodeType: string;
  name: string | null;
  value: string | null;
  status: string | null;
  attributes: Record<string, string>;
  children: PlanNode[];
}

export async function listPlans(): Promise<PlanSummary[]> {
  const res = await fetch(`${BASE}/plans`);
  if (!res.ok) throw new Error(`Failed to list plans: ${res.status}`);
  return res.json();
}

export async function getPlan(id: string): Promise<PlanNode> {
  const res = await fetch(`${BASE}/plan/${id}`);
  if (!res.ok) throw new Error(`Failed to get plan: ${res.status}`);
  return res.json();
}

// --- Node / Workspace types ---

export interface NodeDetail {
  node: {
    id: string;
    projectId: string;
    nodeType: string;
    name: string | null;
    value: string | null;
    parentId: string | null;
    siblingOrder: number;
    createdAt: string;
    modifiedAt: string;
    modifiedBy: string | null;
  };
  attributes: Record<string, string>;
  breadcrumb: { id: string; nodeType: string; name: string | null }[];
  childCount: number;
}

export interface NodeChild {
  id: string;
  nodeType: string;
  name: string | null;
  summary: string | null;
  siblingOrder: number;
  createdAt: string;
  modifiedAt: string;
  status: string | null;
}

export interface NodeEdge {
  edgeType: string;
  targetId: string | null;
  targetName: string | null;
  targetType: string | null;
  direction: 'incoming' | 'outgoing';
}

export interface RootNode {
  id: string;
  nodeType: string;
  name: string | null;
  projectId: string;
  projectName: string | null;
  createdAt: string;
  modifiedAt: string;
  childCount: number;
}

export async function getRootNodes(): Promise<RootNode[]> {
  const res = await fetch(`${BASE}/nodes/roots`);
  if (!res.ok) throw new Error(`Failed to get root nodes: ${res.status}`);
  return res.json();
}

export async function getNodeDetail(id: string): Promise<NodeDetail> {
  const res = await fetch(`${BASE}/node/${id}`);
  if (!res.ok) throw new Error(`Failed to get node: ${res.status}`);
  return res.json();
}

export async function getNodeChildren(id: string): Promise<NodeChild[]> {
  const res = await fetch(`${BASE}/node/${id}/children`);
  if (!res.ok) throw new Error(`Failed to get children: ${res.status}`);
  return res.json();
}

export async function getNodeEdges(id: string): Promise<NodeEdge[]> {
  const res = await fetch(`${BASE}/node/${id}/edges`);
  if (!res.ok) throw new Error(`Failed to get edges: ${res.status}`);
  return res.json();
}

export async function scopedChat(
  nodeId: string,
  message: string,
  conversationId: string | null,
  callbacks: StreamCallbacks,
): Promise<void> {
  return streamChat(message, conversationId, callbacks, `${BASE}/node/${nodeId}/chat`);
}

// --- Node mutation functions ---

export async function updateNode(
  id: string,
  data: { name?: string | null; value?: string | null },
): Promise<NodeDetail> {
  const res = await fetch(`${BASE}/node/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`Failed to update node: ${res.status}`);
  return res.json();
}

export async function updateAttributes(
  id: string,
  attributes: Record<string, string>,
): Promise<Record<string, string>> {
  const res = await fetch(`${BASE}/node/${id}/attributes`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ attributes }),
  });
  if (!res.ok) throw new Error(`Failed to update attributes: ${res.status}`);
  return res.json();
}

export async function createChildNode(
  parentId: string,
  data: {
    nodeType: string;
    name?: string | null;
    value?: string | null;
    attributes?: Record<string, string>;
  },
): Promise<NodeDetail> {
  const res = await fetch(`${BASE}/node/${parentId}/children`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`Failed to create child node: ${res.status}`);
  return res.json();
}

// --- Code decompose/materialize ---

export interface DecomposeResult {
  rootNodeId: string;
  fileId: string;
  filePath: string;
  nodeCount: number;
  calls: number;
  references: number;
}

export interface CodeFile {
  id: string;
  filePath: string;
  rootNodeId: string | null;
  nodeCount: number;
}

export async function decomposeCode(
  projectId: string,
  filePath: string,
  sourceText?: string,
): Promise<DecomposeResult> {
  const res = await fetch(`${BASE}/code/decompose`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ projectId, filePath, sourceText }),
  });
  if (!res.ok) throw new Error(`Failed to decompose: ${res.status}`);
  return res.json();
}

export async function materializeCode(
  fileId?: string,
  rootNodeId?: string,
): Promise<{ source: string }> {
  const res = await fetch(`${BASE}/code/materialize`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fileId, rootNodeId }),
  });
  if (!res.ok) throw new Error(`Failed to materialize: ${res.status}`);
  return res.json();
}

export async function listCodeFiles(projectId: string): Promise<CodeFile[]> {
  const res = await fetch(`${BASE}/code/files/${projectId}`);
  if (!res.ok) throw new Error(`Failed to list files: ${res.status}`);
  return res.json();
}

// --- Permission types ---

export const PERMISSION_LEVELS = ['Observe', 'Suggest', 'Assist', 'Auto'] as const;
export type PermissionLabel = typeof PERMISSION_LEVELS[number];

export interface PermissionConfig {
  defaultLevel: number;
  defaultLabel: string;
  modelOverrides: Record<string, number>;
  systemDefault: number;
}

export interface PendingAction {
  id: string;
  description: string | null;
  paramsJson: string | null;
  createdAt: string;
  requestedBy: string | null;
  status: string;
  skill: string | null;
  model: string | null;
}

export async function getPermissions(conversationId: string): Promise<PermissionConfig> {
  const res = await fetch(`${BASE}/permissions/${conversationId}`);
  if (!res.ok) throw new Error(`Failed to get permissions: ${res.status}`);
  return res.json();
}

export async function setPermission(
  conversationId: string,
  level: number,
  model?: string,
): Promise<PermissionConfig> {
  const res = await fetch(`${BASE}/permissions/${conversationId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ level, model: model ?? null }),
  });
  if (!res.ok) throw new Error(`Failed to set permission: ${res.status}`);
  return res.json();
}

export async function listPendingActions(conversationId: string): Promise<PendingAction[]> {
  const res = await fetch(`${BASE}/actions/${conversationId}`);
  if (!res.ok) throw new Error(`Failed to list actions: ${res.status}`);
  return res.json();
}

export async function approveAction(actionId: string): Promise<void> {
  await fetch(`${BASE}/action/${actionId}/approve`, { method: 'POST' });
}

export async function denyAction(actionId: string): Promise<void> {
  await fetch(`${BASE}/action/${actionId}/deny`, { method: 'POST' });
}

// --- System message event stream ---

export interface SystemMessageEvent {
  level: string;
  text: string;
  conversationId?: string;
}

export function subscribeToEvents(onSystemMessage: (msg: SystemMessageEvent) => void): EventSource {
  const es = new EventSource(`${BASE}/events`);

  es.addEventListener('system_message', (e) => {
    const data: SystemMessageEvent = JSON.parse(e.data);
    onSystemMessage(data);
  });

  es.onerror = () => {
    // EventSource auto-reconnects; log for debugging
    console.warn('System event stream disconnected, reconnecting...');
  };

  return es;
}
