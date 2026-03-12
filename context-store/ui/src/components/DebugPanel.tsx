// DebugPanel — shows pipeline breakdown and preview for messages
//
// Two modes:
//   Debug: shows how each sent message was processed (parse, intent, context, timing)
//   Preview: dry-runs the pipeline on the current input without sending
//
// Depends on: api.ts types
// Used by: App.tsx

import { useState, useEffect } from 'react';
import type { DebugData, PreviewResult, InterpretData } from '../api';

interface Props {
  // Debug data keyed by message index (from streaming)
  debugLog: Record<number, DebugData>;
  // Preview result from dry-run
  preview: PreviewResult | null;
  previewLoading: boolean;
}

export default function DebugPanel({ debugLog, preview, previewLoading }: Props) {
  const sortedKeys = Object.keys(debugLog).map(Number).filter(k => !isNaN(k)).sort((a, b) => a - b);
  const count = sortedKeys.length;

  // Track which entry we're viewing. Default to latest (last index).
  const [viewIndex, setViewIndex] = useState(count > 0 ? count - 1 : 0);

  // Auto-follow latest when new entries arrive
  useEffect(() => {
    if (count > 0) setViewIndex(count - 1);
  }, [count]);

  const currentKey = sortedKeys[viewIndex];
  const currentDebug = currentKey != null ? debugLog[currentKey] : undefined;

  return (
    <div className="h-full overflow-y-auto p-3">
      <h2 className="text-sm font-semibold text-slate-400 uppercase tracking-wider mb-3">
        Pipeline Debug
      </h2>

      {/* Preview section */}
      {previewLoading && (
        <div className="text-sm text-slate-400 animate-pulse mb-4">Analyzing...</div>
      )}

      {preview && !previewLoading && (
        <div className="space-y-3 mb-4">
          <div className="border border-cyan-500/30 rounded-lg p-2 bg-cyan-900/10">
            <div className="text-xs text-cyan-400 font-semibold mb-1">Preview</div>
            <PipelineSteps
              parse={{ ...preview.parse, durationMs: 0 }}
              intent={preview.intent}
              context={{ ...preview.context, durationMs: 0 }}
              prompt={{ systemPromptLength: 0, userPromptLength: 0, tokenEstimate: preview.context.tokenEstimate }}
            />
            {preview.promptPreview && (
              <div className="mt-2">
                <div className="text-xs text-slate-500 mb-1">System prompt preview</div>
                <div className="text-xs text-slate-400 bg-slate-800/50 rounded p-2 font-mono whitespace-pre-wrap max-h-48 overflow-y-auto">
                  {preview.promptPreview}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Debug log with navigation */}
      {currentDebug && (
        <div className="space-y-3">
          {count > 1 && (
            <div className="flex items-center justify-between">
              <button
                onClick={() => setViewIndex(i => Math.max(0, i - 1))}
                disabled={viewIndex === 0}
                className="text-xs px-2 py-0.5 rounded border border-slate-600 text-slate-400 hover:bg-slate-700 disabled:opacity-30 disabled:cursor-not-allowed"
              >
                Prev
              </button>
              <span className="text-xs text-slate-500">
                {viewIndex + 1} / {count}
              </span>
              <button
                onClick={() => setViewIndex(i => Math.min(count - 1, i + 1))}
                disabled={viewIndex === count - 1}
                className="text-xs px-2 py-0.5 rounded border border-slate-600 text-slate-400 hover:bg-slate-700 disabled:opacity-30 disabled:cursor-not-allowed"
              >
                Next
              </button>
            </div>
          )}
          <div className="border border-amber-500/30 rounded-lg p-2 bg-amber-900/10">
            <div className="text-xs text-amber-400 font-semibold mb-1">
              Message {currentKey + 1} Debug
            </div>
            <PipelineSteps
              parse={currentDebug.parse}
              interpret={currentDebug.interpret}
              intent={currentDebug.intent}
              context={currentDebug.context}
              prompt={currentDebug.prompt}
              timing={currentDebug.timing}
            />
          </div>
        </div>
      )}

      {!preview && !currentDebug && !previewLoading && (
        <p className="text-sm text-slate-500 italic">
          Type a message and pause to see a preview, or send to see debug info
        </p>
      )}
    </div>
  );
}

function PipelineSteps({
  parse,
  interpret,
  intent,
  context,
  prompt,
  timing,
}: {
  parse?: DebugData['parse'];
  interpret?: InterpretData;
  intent?: DebugData['intent'];
  context?: DebugData['context'];
  prompt?: DebugData['prompt'];
  timing?: DebugData['timing'];
}) {
  return (
    <div className="space-y-2 text-xs">
      {/* Step 1: Parse */}
      {parse && (
        <Step number={1} label="Parse" duration={parse.durationMs}>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="bg-purple-600/20 border border-purple-500/30 text-purple-300 px-1.5 py-0.5 rounded">
              {parse.mention}
            </span>
            <span className="text-slate-500">&rarr;</span>
            <span className="text-slate-300">{parse.model?.display || 'unknown'}</span>
            <span className="text-slate-600">({parse.model?.provider})</span>
          </div>
          {parse.cleanedMessage !== parse.originalMessage && (
            <div className="text-slate-500 mt-1 truncate" title={parse.cleanedMessage}>
              Cleaned: &ldquo;{parse.cleanedMessage}&rdquo;
            </div>
          )}
        </Step>
      )}

      {/* Step 2: Interpreter — the role's structured understanding */}
      {interpret ? (
        <Step number={2} label="Interpret" duration={interpret.durationMs}>
          {/* Project resolution */}
          <div className="flex items-center gap-2 flex-wrap mb-1">
            <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
              interpret.project.type === 'new'
                ? 'bg-green-600/20 text-green-400 border border-green-500/30'
                : interpret.project.type === 'cross_project'
                ? 'bg-cyan-600/20 text-cyan-400 border border-cyan-500/30'
                : 'bg-slate-700/50 text-slate-400'
            }`}>
              {interpret.project.type}
            </span>
            {interpret.project.name && (
              <span className="text-slate-300">{interpret.project.name}</span>
            )}
          </div>
          {/* Intent + confidence */}
          <div className="flex items-center gap-2 mb-1">
            <span className="bg-indigo-600/20 border border-indigo-500/30 text-indigo-300 px-1.5 py-0.5 rounded">
              {interpret.intent}
            </span>
            <span className={`text-[10px] font-mono ${
              interpret.confidence >= 0.8 ? 'text-green-400' : interpret.confidence >= 0.6 ? 'text-yellow-400' : 'text-red-400'
            }`}>
              {(interpret.confidence * 100).toFixed(0)}%
            </span>
            {interpret.isRegexFallback && (
              <span className="bg-yellow-600/20 text-yellow-400 border border-yellow-500/30 px-1 py-0.5 rounded text-[10px]">
                regex fallback
              </span>
            )}
          </div>
          {/* Reasoning */}
          {interpret.reasoning && (
            <div className="text-slate-500 mt-1 italic" title={interpret.reasoning}>
              {interpret.reasoning}
            </div>
          )}
          {/* Resolved entities */}
          {interpret.entities.length > 0 && (
            <div className="mt-1.5 space-y-0.5">
              {interpret.entities.map((e, i) => (
                <div key={i} className="flex items-center gap-1.5">
                  <span className="text-slate-500">&rarr;</span>
                  <span className="text-slate-400">&ldquo;{e.mention}&rdquo;</span>
                  {e.nodeId && (
                    <span className="bg-slate-700/50 text-slate-500 px-1 py-0.5 rounded text-[10px] font-mono">
                      {e.nodeType || 'node'}
                    </span>
                  )}
                  {e.nodeName && (
                    <span className="text-slate-300 truncate">{e.nodeName}</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </Step>
      ) : intent && (
        /* Fallback: legacy intent display if no interpret data */
        <Step number={2} label="Intent">
          <div className="flex items-center gap-2">
            <span className="bg-indigo-600/20 border border-indigo-500/30 text-indigo-300 px-1.5 py-0.5 rounded">
              {intent.intent}
            </span>
            <span className="text-slate-500">conf: {intent.confidence}</span>
            {intent.pattern && (
              <span className="text-slate-600 truncate" title={intent.pattern}>
                &ldquo;{intent.pattern}&rdquo;
              </span>
            )}
          </div>
        </Step>
      )}

      {/* Step 3: Context */}
      {context && (
        <Step number={3} label="Context" duration={context.durationMs}>
          <div className="flex items-center gap-3">
            <span className="text-slate-300">
              {context.nodeCount} node{context.nodeCount !== 1 ? 's' : ''}
            </span>
            {context.coverage && (
              <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                context.coverage.Pct >= 80
                  ? 'bg-green-600/20 text-green-400 border border-green-500/30'
                  : context.coverage.Pct >= 50
                  ? 'bg-yellow-600/20 text-yellow-400 border border-yellow-500/30'
                  : 'bg-red-600/20 text-red-400 border border-red-500/30'
              }`}>
                {context.coverage.Embedded}/{context.coverage.Total} embedded ({Math.round(context.coverage.Pct)}%)
              </span>
            )}
          </div>
          {context.nodes && context.nodes.length > 0 && (
            <div className="mt-1.5 space-y-1">
              {context.nodes.map((n, i) => (
                <div key={i} className="flex items-center gap-1.5 group">
                  <span className="bg-slate-700/50 text-slate-500 px-1 py-0.5 rounded text-[10px] font-mono flex-shrink-0">
                    {n.nodeType}
                  </span>
                  <span className="text-slate-400 truncate flex-1 min-w-0" title={n.name || '(unnamed)'}>
                    {n.name || '(unnamed)'}
                  </span>
                  {n.score != null && (
                    <span className={`flex-shrink-0 text-[10px] font-mono px-1 py-0.5 rounded ${
                      n.score < 0.4
                        ? 'bg-green-600/20 text-green-400'
                        : n.score < 0.5
                        ? 'bg-yellow-600/20 text-yellow-400'
                        : 'bg-slate-700/50 text-slate-500'
                    }`}>
                      {n.score.toFixed(3)}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </Step>
      )}

      {/* Step 4: Prompt */}
      {prompt && prompt.tokenEstimate > 0 && (
        <Step number={4} label="Prompt">
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-3 text-slate-300">
              <span className={prompt.naiveTokenEstimate && prompt.naiveTokenEstimate > prompt.tokenEstimate ? 'text-emerald-400' : ''}>
                ~{prompt.tokenEstimate.toLocaleString()} tokens
              </span>
              <span className="text-slate-600 text-[10px]">
                sys: {prompt.systemPromptLength} + user: {prompt.userPromptLength} chars
              </span>
            </div>
            {prompt.naiveTokenEstimate != null && prompt.naiveTokenEstimate > prompt.tokenEstimate && (() => {
              const saved = prompt.naiveTokenEstimate! - prompt.tokenEstimate;
              const pct = Math.round((saved / prompt.naiveTokenEstimate!) * 100);
              return (
                <div className="text-[10px] text-emerald-600">
                  saved {saved.toLocaleString()} ({pct}%) vs naive ~{prompt.naiveTokenEstimate!.toLocaleString()}
                </div>
              );
            })()}
          </div>
        </Step>
      )}

      {/* Step 5: Timing */}
      {timing && (
        <Step number={5} label="Timing">
          <div className="space-y-0.5">
            <TimingBar label="Parse" ms={timing.parseMs} total={timing.totalMs} />
            <TimingBar label="Interpret" ms={timing.classifyMs} total={timing.totalMs} />
            <TimingBar label="Context" ms={timing.contextMs} total={timing.totalMs} />
            <TimingBar label="Generate" ms={timing.generateMs} total={timing.totalMs} />
            <TimingBar label="Extract" ms={timing.extractMs} total={timing.totalMs} />
            <div className="flex items-center gap-2 pt-1 border-t border-slate-700/50">
              <span className="text-slate-300 font-medium w-16">Total</span>
              <span className="text-slate-300 font-medium">{(timing.totalMs / 1000).toFixed(1)}s</span>
            </div>
          </div>
        </Step>
      )}
    </div>
  );
}

function TimingBar({ label, ms, total }: { label: string; ms: number; total: number }) {
  const pct = total > 0 ? (ms / total) * 100 : 0;
  return (
    <div className="flex items-center gap-2">
      <span className="text-slate-500 w-16 flex-shrink-0">{label}</span>
      <div className="flex-1 bg-slate-800 rounded-full h-1.5 min-w-0">
        <div
          className="bg-cyan-500/60 h-1.5 rounded-full transition-all"
          style={{ width: `${Math.max(pct, 1)}%` }}
        />
      </div>
      <span className="text-slate-600 w-12 text-right flex-shrink-0 text-[10px] font-mono">{ms}ms</span>
    </div>
  );
}

function Step({
  number,
  label,
  duration,
  children,
}: {
  number: number;
  label: string;
  duration?: number;
  children: React.ReactNode;
}) {
  return (
    <div className="border-l-2 border-slate-700 pl-2">
      <div className="flex items-center gap-1.5 mb-0.5">
        <span className="bg-slate-700 text-slate-400 w-4 h-4 rounded-full flex items-center justify-center text-[10px] font-bold flex-shrink-0">
          {number}
        </span>
        <span className="text-slate-400 font-medium">{label}</span>
        {duration != null && duration > 0 && (
          <span className="text-slate-600 ml-auto text-[10px] font-mono">{duration}ms</span>
        )}
      </div>
      <div className="ml-5">{children}</div>
    </div>
  );
}
