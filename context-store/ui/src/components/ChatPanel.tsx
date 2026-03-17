// ChatPanel — conversation view with concurrent SSE streaming and @model routing
//
// Shows messages with speaker badges, input box, streaming display.
// Supports @model mentions: @sonnet, @opus, @haiku, @gemini, @flash, @pro, @codex, @gpt, @ollama, @qwen
// Multiple streams can run concurrently with per-stream status cards.
// Debounced preview fires while typing (when debug panel is open).
// Depends on: api.ts
// Used by: App.tsx

import { useState, useRef, useEffect } from 'react';
import {
  streamChat, previewMessage, approveAction, denyAction,
  type ChatMessage, type ExtractedItem, type DebugData,
  type PreviewResult, type PermissionRequestEvent,
} from '../api';
import { MODEL_BORDER_COLORS } from '../colors';

interface PendingApproval extends PermissionRequestEvent {
  status: 'pending' | 'approved' | 'denied';
}

interface Props {
  conversationId: string | null;
  messages: ChatMessage[];
  onConversationId: (id: string) => void;
  onNewMessage: (targetId: string | null, msg: ChatMessage) => void;
  onUpdateMessage: (targetId: string | null, messageId: string, updates: Partial<ChatMessage>) => void;
  onExtraction: (targetId: string | null, items: ExtractedItem[]) => void;
  onIntentUpdate: (targetId: string | null, intent: string, confidence: string) => void;
  onModelUpdate: (targetId: string | null, model: string) => void;
  onDebugData: (targetId: string | null, messageIndex: number, data: DebugData) => void;
  onStreamingChange: (targetId: string | null, streaming: boolean) => void;
  onPreview: (result: PreviewResult | null) => void;
  onPreviewLoading: (loading: boolean) => void;
  debugOpen: boolean;
}

const MODEL_OPTIONS = [
  { mention: '@sonnet', label: 'Sonnet', desc: 'Claude Sonnet', color: 'bg-purple-600' },
  { mention: '@opus', label: 'Opus', desc: 'Claude Opus', color: 'bg-purple-800' },
  { mention: '@haiku', label: 'Haiku', desc: 'Claude Haiku (fast)', color: 'bg-purple-400' },
  { mention: '@gemini', label: 'Gemini', desc: 'Gemini 2.5 Flash', color: 'bg-blue-600' },
  { mention: '@flash', label: 'Flash', desc: 'Gemini 2.5 Flash', color: 'bg-blue-500' },
  { mention: '@pro', label: 'Pro', desc: 'Gemini 2.5 Pro', color: 'bg-blue-700' },
  { mention: '@codex', label: 'Codex', desc: 'GPT-4.1', color: 'bg-green-600' },
  { mention: '@gpt', label: 'GPT', desc: 'GPT-4.1', color: 'bg-green-600' },
  { mention: '@ollama', label: 'Ollama', desc: 'qwen3.5:4b (local)', color: 'bg-orange-600' },
  { mention: '@qwen', label: 'Qwen', desc: 'qwen3.5:4b (local)', color: 'bg-orange-600' },
];

type StreamPhase = 'parsing' | 'interpreting' | 'assembling' | 'generating' | 'calling_tool' | 'extracting';

const PHASE_LABELS: Record<StreamPhase, string> = {
  parsing: 'Parsing message...',
  interpreting: 'Interpreting intent...',
  assembling: 'Assembling context...',
  generating: 'Generating response...',
  calling_tool: 'Calling tool...',
  extracting: 'Extracting ideas...',
};

interface ActiveStream {
  id: string;
  model: string;
  provider: string;
  topic: string;
  phase: StreamPhase;
  intent: string | null;
  buffer: string;
  toolName: string | null;
}

export default function ChatPanel({
  conversationId,
  messages,
  onConversationId,
  onNewMessage,
  onUpdateMessage,
  onExtraction,
  onIntentUpdate,
  onModelUpdate,
  onDebugData,
  onStreamingChange,
  onPreview,
  onPreviewLoading,
  debugOpen,
}: Props) {
  const [input, setInput] = useState('');
  const [activeStreams, setActiveStreams] = useState<Map<string, ActiveStream>>(new Map());
  const [pendingApprovals, setPendingApprovals] = useState<PendingApproval[]>([]);
  const [showAutocomplete, setShowAutocomplete] = useState(false);
  const [autocompleteIndex, setAutocompleteIndex] = useState(0);
  const [mentionFilter, setMentionFilter] = useState('');
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const previewTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const messageIndexRef = useRef(0);

  const hasActiveStreams = activeStreams.size > 0;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, activeStreams]);

  useEffect(() => {
    messageIndexRef.current = messages.length;
  }, [messages.length]);

  const filteredModels = showAutocomplete
    ? MODEL_OPTIONS.filter((m) => m.mention.toLowerCase().startsWith(`@${mentionFilter.toLowerCase()}`))
    : [];

  const handleInputChange = (value: string) => {
    setInput(value);
    const cursorPos = inputRef.current?.selectionStart ?? value.length;
    const textBeforeCursor = value.slice(0, cursorPos);
    const atIndex = textBeforeCursor.lastIndexOf('@');

    if (atIndex !== -1) {
      const afterAt = textBeforeCursor.slice(atIndex + 1);
      const beforeAt = atIndex === 0 ? '' : textBeforeCursor[atIndex - 1];
      if ((atIndex === 0 || beforeAt === ' ') && !/\s/.test(afterAt)) {
        setMentionFilter(afterAt);
        setShowAutocomplete(true);
        setAutocompleteIndex(0);
        return;
      }
    }
    setShowAutocomplete(false);
  };

  const selectModel = (mention: string) => {
    const cursorPos = inputRef.current?.selectionStart ?? input.length;
    const textBeforeCursor = input.slice(0, cursorPos);
    const atIndex = textBeforeCursor.lastIndexOf('@');
    const before = input.slice(0, atIndex);
    const after = input.slice(cursorPos);
    setInput(`${before}${mention} ${after}`);
    setShowAutocomplete(false);
    setTimeout(() => {
      inputRef.current?.focus();
      const newPos = before.length + mention.length + 1;
      inputRef.current?.setSelectionRange(newPos, newPos);
    }, 0);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (showAutocomplete && filteredModels.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setAutocompleteIndex((i) => (i + 1) % filteredModels.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setAutocompleteIndex((i) => (i - 1 + filteredModels.length) % filteredModels.length);
        return;
      }
      if (e.key === 'Tab' || e.key === 'Enter') {
        e.preventDefault();
        selectModel(filteredModels[autocompleteIndex].mention);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setShowAutocomplete(false);
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      handleSend();
    }
  };

  useEffect(() => {
    if (!debugOpen || !input.trim()) {
      onPreview(null);
      return;
    }
    clearTimeout(previewTimer.current);
    previewTimer.current = setTimeout(async () => {
      onPreviewLoading(true);
      try {
        const result = await previewMessage(input.trim(), conversationId);
        onPreview(result);
      } catch {
        onPreview(null);
      } finally {
        onPreviewLoading(false);
      }
    }, 500);
    return () => clearTimeout(previewTimer.current);
  }, [input, debugOpen, conversationId]);

  // Helper to update a single stream field
  const updateStream = (streamId: string, updates: Partial<ActiveStream>) => {
    setActiveStreams((prev) => {
      const next = new Map(prev);
      const entry = next.get(streamId);
      if (entry) next.set(streamId, { ...entry, ...updates });
      return next;
    });
  };

  const handleSend = async () => {
    if (!input.trim()) return;

    const userMessage = input.trim();
    const streamId = crypto.randomUUID();
    setInput('');
    onPreview(null);

    // Capture the conversation ID at send time. This stream targets this conversation
    // even if the user switches to a different one while it's running.
    // Uses a mutable variable so onConversationId can update it for null→UUID migration.
    let targetId = conversationId;

    // Truncate topic for display (strip @mention, cap at 60 chars)
    const cleaned = userMessage.replace(/@\w+\s*/, '');
    const topic = cleaned.length > 60 ? cleaned.slice(0, 60) + '...' : cleaned;

    const responseId = crypto.randomUUID();

    onNewMessage(targetId, {
      id: crypto.randomUUID(),
      speaker: 'user',
      content: userMessage,
      createdAt: new Date().toISOString(),
    });

    // Reserve a slot for the response — ensures ordering even if user sends another message while streaming
    onNewMessage(targetId, {
      id: responseId,
      speaker: '...',
      content: '',
      createdAt: new Date().toISOString(),
      isPlaceholder: true,
    });

    setActiveStreams((prev) => {
      const next = new Map(prev);
      next.set(streamId, {
        id: streamId,
        model: 'sonnet',
        provider: 'anthropic',
        topic,
        phase: 'parsing',
        intent: null,
        buffer: '',
        toolName: null,
      });
      return next;
    });
    onStreamingChange(targetId, true);

    let fullResponse = '';
    let respondingModel = 'sonnet';
    const debugIndex = messageIndexRef.current;
    const accumulatedDebug: DebugData = {};

    streamChat(userMessage, conversationId, {
      onConversationId: (id) => {
        // Server assigned a real ID — migrate cache and update our target
        onConversationId(id);
        targetId = id;
      },
      onIntent: (data) => {
        onIntentUpdate(targetId, data.intent, data.confidence);
        updateStream(streamId, { phase: 'assembling', intent: data.intent });
      },
      onModel: (data) => {
        respondingModel = data.display;
        onModelUpdate(targetId, data.display);
        updateStream(streamId, { model: data.display, provider: data.provider });
      },
      onToken: (text) => {
        fullResponse += text;
        updateStream(streamId, { buffer: fullResponse, phase: 'generating' });
      },
      onExtraction: (data) => {
        onExtraction(targetId, data.items);
      },
      onPhase: (phase) => {
        updateStream(streamId, { phase: phase as StreamPhase });
      },
      onToolCall: (data) => {
        updateStream(streamId, { phase: 'calling_tool', toolName: data.name });
      },
      onToolResult: () => {
        updateStream(streamId, { toolName: null });
      },
      onPermissionRequest: (data) => {
        setPendingApprovals((prev) => [...prev, { ...data, status: 'pending' }]);
      },
      onDebug: (data) => {
        Object.assign(accumulatedDebug, data);
        onDebugData(targetId, debugIndex, { ...accumulatedDebug });

        // Update phase based on which debug events arrive
        if (data.parse) updateStream(streamId, { phase: 'interpreting' });
        if (data.interpret) updateStream(streamId, { phase: 'assembling', intent: data.interpret.intent });
        if (data.context) updateStream(streamId, { phase: 'generating' });
        if (data.timing) updateStream(streamId, { phase: 'extracting' });
      },
      onDone: () => {
        setActiveStreams((prev) => {
          const next = new Map(prev);
          next.delete(streamId);
          if (next.size === 0) onStreamingChange(targetId, false);
          return next;
        });
        onUpdateMessage(targetId, responseId, {
          speaker: respondingModel,
          content: fullResponse,
          isPlaceholder: false,
        });
      },
      onError: (error) => {
        console.error('Stream error:', error);
        setActiveStreams((prev) => {
          const next = new Map(prev);
          next.delete(streamId);
          if (next.size === 0) onStreamingChange(targetId, false);
          return next;
        });
        onUpdateMessage(targetId, responseId, {
          speaker: 'system:error',
          content: `Stream error from ${respondingModel}: ${error}`,
          isPlaceholder: false,
        });
      },
    }).catch((err) => {
      console.error('Stream failed:', err);
      setActiveStreams((prev) => {
        const next = new Map(prev);
        next.delete(streamId);
        if (next.size === 0) onStreamingChange(targetId, false);
        return next;
      });
      onUpdateMessage(targetId, responseId, {
        speaker: 'system:error',
        content: `Connection lost to ${respondingModel}: ${err?.message || err}`,
        isPlaceholder: false,
      });
    });
  };

  const speakerBadge = (speaker: string) => {
    const colors: Record<string, string> = {
      user: 'bg-blue-600',
      sonnet: 'bg-purple-600',
      claude: 'bg-purple-600',
      opus: 'bg-purple-800',
      haiku: 'bg-purple-400',
      gemini: 'bg-blue-600',
      flash: 'bg-blue-500',
      pro: 'bg-blue-700',
      codex: 'bg-green-600',
      gpt: 'bg-green-600',
      ollama: 'bg-orange-600',
      qwen: 'bg-orange-600',
      'system:info': 'bg-green-600',
      'system:warn': 'bg-yellow-600',
      'system:error': 'bg-red-600',
    };
    const label = speaker.startsWith('system:') ? 'system' : speaker;
    return (
      <span className={`${colors[speaker] || 'bg-gray-600'} text-xs px-2 py-0.5 rounded-full font-medium`}>
        {label}
      </span>
    );
  };

  return (
    <div className="flex flex-col h-full">
      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.length === 0 && !hasActiveStreams && (
          <div className="text-center text-slate-500 mt-20">
            <p className="text-lg">Start a conversation</p>
            <p className="text-sm mt-1">Type an idea, question, or topic to explore</p>
            <p className="text-xs mt-3 text-slate-600">
              Type @ to pick a model: {MODEL_OPTIONS.map(m => m.mention).join(', ')}
            </p>
          </div>
        )}

        {messages.filter((msg) => !(msg.isPlaceholder && !msg.content)).map((msg) => (
          <div key={msg.id} className={`flex gap-3 ${msg.speaker.startsWith('system:') ? 'opacity-80' : ''}`}>
            <div className="flex-shrink-0 mt-1">{speakerBadge(msg.speaker)}</div>
            <div className={`whitespace-pre-wrap leading-relaxed ${
              msg.speaker.startsWith('system:') ? 'text-slate-400 italic text-sm' : 'text-slate-200'
            }`}>{msg.content}</div>
          </div>
        ))}

        {/* Active streaming responses — status cards */}
        {Array.from(activeStreams.values()).map((stream) => (
          <div
            key={stream.id}
            className={`border rounded-lg p-3 ${MODEL_BORDER_COLORS[stream.model] || 'border-slate-600/40'} bg-slate-800/30`}
          >
            {/* Stream header: model + topic + phase */}
            <div className="flex items-center gap-2 mb-2">
              {speakerBadge(stream.model)}
              <span className="text-xs text-slate-500">{stream.provider}</span>
              <span className="text-xs text-slate-600 mx-1">&middot;</span>
              <span className="text-xs text-slate-400 truncate flex-1" title={stream.topic}>
                {stream.topic}
              </span>
              {stream.intent && (
                <span className="bg-indigo-600/20 border border-indigo-500/30 text-indigo-300 text-[10px] px-1.5 py-0.5 rounded">
                  {stream.intent}
                </span>
              )}
            </div>

            {/* Phase indicator */}
            <div className="flex items-center gap-2 mb-2">
              <div className="flex gap-1">
                {(['parsing', 'interpreting', 'assembling', 'generating', 'extracting'] as StreamPhase[]).map((p) => {
                  const phases: StreamPhase[] = ['parsing', 'interpreting', 'assembling', 'generating', 'extracting'];
                  const effectivePhase = stream.phase === 'calling_tool' ? 'generating' : stream.phase;
                  const idx = phases.indexOf(p);
                  const currentIdx = phases.indexOf(effectivePhase);
                  return (
                    <div
                      key={p}
                      className={`h-1 w-6 rounded-full transition-colors ${
                        idx === currentIdx
                          ? 'bg-blue-400 animate-pulse'
                          : idx < currentIdx
                            ? 'bg-blue-600'
                            : 'bg-slate-700'
                      }`}
                    />
                  );
                })}
              </div>
              <span className="text-[11px] text-slate-500">
                {stream.phase === 'calling_tool' && stream.toolName
                  ? `Calling ${stream.toolName}...`
                  : PHASE_LABELS[stream.phase]}
              </span>
            </div>

            {/* Response content */}
            {stream.buffer ? (
              <div className="text-slate-200 whitespace-pre-wrap leading-relaxed text-sm">
                {stream.buffer}
                <span className="animate-pulse">|</span>
              </div>
            ) : stream.phase === 'generating' ? (
              <div className="text-slate-400 animate-pulse text-sm">Waiting for first token...</div>
            ) : null}
          </div>
        ))}

        {/* Pending approval cards */}
        {pendingApprovals.filter((a) => a.status === 'pending').map((action) => (
          <div
            key={action.actionId}
            className="border border-amber-500/30 bg-amber-900/10 rounded-lg p-3"
          >
            <div className="flex items-center gap-2 mb-2">
              <span className="text-amber-400 text-xs font-medium">Permission Request</span>
              <span className="bg-slate-700 text-xs px-1.5 py-0.5 rounded text-slate-300">
                {action.model}
              </span>
            </div>
            <div className="text-sm text-slate-300 mb-2">
              wants to call <span className="font-mono text-amber-300">{action.skill}</span>
            </div>
            <div className="text-xs text-slate-400 mb-3">{action.description}</div>
            <div className="flex items-center gap-2">
              <button
                onClick={async () => {
                  await approveAction(action.actionId);
                  setPendingApprovals((prev) =>
                    prev.map((a) => a.actionId === action.actionId ? { ...a, status: 'approved' } : a),
                  );
                }}
                className="bg-green-600 hover:bg-green-500 text-white text-xs px-3 py-1 rounded transition-colors"
              >
                Approve
              </button>
              <button
                onClick={async () => {
                  await denyAction(action.actionId);
                  setPendingApprovals((prev) =>
                    prev.map((a) => a.actionId === action.actionId ? { ...a, status: 'denied' } : a),
                  );
                }}
                className="bg-red-600/80 hover:bg-red-500 text-white text-xs px-3 py-1 rounded transition-colors"
              >
                Deny
              </button>
            </div>
          </div>
        ))}

        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="border-t border-slate-700 p-4">
        <div className="relative">
          {showAutocomplete && filteredModels.length > 0 && (
            <div className="absolute bottom-full mb-1 left-0 w-72 bg-slate-800 border border-slate-600 rounded-lg shadow-xl overflow-hidden z-10">
              {filteredModels.map((model, i) => (
                <button
                  key={model.mention}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    selectModel(model.mention);
                  }}
                  className={`w-full flex items-center gap-3 px-3 py-2 text-left transition-colors ${
                    i === autocompleteIndex
                      ? 'bg-slate-700'
                      : 'hover:bg-slate-750 hover:bg-slate-700/50'
                  }`}
                >
                  <span className={`${model.color} text-xs px-2 py-0.5 rounded-full font-medium`}>
                    {model.label}
                  </span>
                  <div className="flex-1 min-w-0">
                    <span className="text-sm text-slate-300">{model.mention}</span>
                    <span className="text-xs text-slate-500 ml-2">{model.desc}</span>
                  </div>
                </button>
              ))}
              <div className="px-3 py-1 text-[10px] text-slate-600 border-t border-slate-700">
                Tab or Enter to select &middot; Esc to close
              </div>
            </div>
          )}

          <div className="flex gap-2">
            <input
              ref={inputRef}
              type="text"
              value={input}
              onChange={(e) => handleInputChange(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Type @ to pick a model, then your message..."
              className="flex-1 bg-slate-800 border border-slate-600 rounded-lg px-4 py-2
                         text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500"
            />
            <button
              onClick={handleSend}
              disabled={!input.trim()}
              className="bg-blue-600 hover:bg-blue-500 disabled:bg-slate-700 disabled:text-slate-500
                         text-white px-4 py-2 rounded-lg font-medium transition-colors"
            >
              Send
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
