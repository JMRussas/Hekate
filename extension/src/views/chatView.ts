//  Beethoven VSCode Extension - Chat WebviewView Provider
//
//  Renders an inline chat panel in the sidebar below the fleet tree.
//  Supports slash commands (/status, /tasks, /start, /pause) and
//  free-text chat routed to the orchestration API via BeethovenClient.
//
//  Depends on: ../api/client.ts (BeethovenClient)
//  Used by:    extension.ts

import * as vscode from "vscode";
import { spawn } from "child_process";
import { ClaudeCode } from "claude-code-js";
import { BeethovenClient } from "../api/client";
import { redactSecrets } from "../redact";

export class ChatViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "beethovenChat";

  private _view?: vscode.WebviewView;
  private _activeProjectId?: string;
  private _activeProjectName?: string;

  private _client: BeethovenClient;
  private _ollamaUrl: string;
  private _ollamaModel: string;
  private static readonly MAX_HISTORY = 100;
  private _chatHistory: Array<{ role: string; content: string }> = [];
  private _abortController?: AbortController;
  private _claudeSDK: InstanceType<typeof ClaudeCode>;
  private _claudeSessionId?: string;
  private _globalState: vscode.Memento;

  constructor(
    private readonly _extensionUri: vscode.Uri,
    client: BeethovenClient,
    ollamaUrl: string,
    ollamaModel: string,
    globalState: vscode.Memento
  ) {
    this._client = client;
    this._ollamaUrl = ollamaUrl;
    this._ollamaModel = ollamaModel;
    this._claudeSDK = new ClaudeCode();
    this._globalState = globalState;
  }

  public updateClient(client: BeethovenClient): void {
    this._client = client;
  }

  public updateOllama(url: string, model: string): void {
    this._ollamaUrl = url;
    this._ollamaModel = model;
  }

  public resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ): void {
    this._view = webviewView;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this._extensionUri],
    };

    // Register listener before setting HTML so we catch the webviewReady message
    webviewView.webview.onDidReceiveMessage(async (msg) => {
      switch (msg.type) {
        case "sendMessage":
          await this._handleChatMessage(msg.text, msg.provider ?? "gemini", msg.model);
          break;
        case "slashCommand":
          await this._handleSlashCommand(msg.command, msg.args);
          break;
        case "stopGeneration":
          this._abortController?.abort();
          break;
        case "providerChanged":
          this._globalState.update("beethoven.lastSelection", msg.value);
          break;
        case "webviewReady": {
          const saved = this._globalState.get<string>("beethoven.lastSelection");
          if (saved) {
            this.postMessage({ type: "restoreSelection", value: saved });
          }
          break;
        }
      }
    });

    webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);
  }

  /** Scope chat to a specific project. Updates the header in the webview. */
  public setActiveProject(projectId: string, projectName: string): void {
    this._activeProjectId = projectId;
    this._activeProjectName = projectName;
    this.postMessage({ type: "setProject", name: projectName });
  }

  /** Inject a system-level message into the chat (e.g. from SSE events). */
  public addSystemMessage(text: string): void {
    this.postMessage({ type: "addMessage", role: "system", content: text });
  }

  /** Send an arbitrary message to the webview. */
  public postMessage(msg: Record<string, unknown>): void {
    this._view?.webview.postMessage(msg);
  }

  // ── Slash command handling ──────────────────────────────────────────

  private async _handleSlashCommand(
    command: string,
    args?: string
  ): Promise<void> {
    try {
      switch (command) {
        case "status": {
          const projects = await this._client.listProjects();
          const lines = projects.map(
            (p) => `${p.name}: ${p.status}`
          );
          this.postMessage({
            type: "addMessage",
            role: "assistant",
            content: lines.length
              ? lines.join("\n")
              : "No projects found.",
          });
          break;
        }
        case "tasks": {
          if (!this._activeProjectId) {
            this.postMessage({
              type: "addMessage",
              role: "assistant",
              content: "No project selected. Use /status or select a project first.",
            });
            return;
          }
          const detail = await this._client.getProject(this._activeProjectId);
          const taskLines = detail.tasks.map(
            (t) => `[${t.status}] ${t.title}`
          );
          this.postMessage({
            type: "addMessage",
            role: "assistant",
            content: taskLines.length
              ? taskLines.join("\n")
              : "No tasks in this project.",
          });
          break;
        }
        case "start": {
          if (!this._activeProjectId) {
            this.postMessage({
              type: "addMessage",
              role: "assistant",
              content: "No project selected.",
            });
            return;
          }
          await this._client.startProject(this._activeProjectId);
          this.postMessage({
            type: "addMessage",
            role: "assistant",
            content: `Started project: ${this._activeProjectName}`,
          });
          break;
        }
        case "pause": {
          if (!this._activeProjectId) {
            this.postMessage({
              type: "addMessage",
              role: "assistant",
              content: "No project selected.",
            });
            return;
          }
          await this._client.pauseProject(this._activeProjectId);
          this.postMessage({
            type: "addMessage",
            role: "assistant",
            content: `Paused project: ${this._activeProjectName}`,
          });
          break;
        }
        case "refresh": {
          vscode.commands.executeCommand("beethoven.refresh");
          this.postMessage({
            type: "addMessage",
            role: "assistant",
            content: "Fleet refreshed.",
          });
          break;
        }
        case "help": {
          this.postMessage({
            type: "addMessage",
            role: "assistant",
            content: "Available commands:\n/status — list all projects\n/tasks — show tasks for selected project\n/start — start selected project\n/pause — pause selected project\n/refresh — refresh fleet tree\n/help — show this message",
          });
          break;
        }
        default:
          this.postMessage({
            type: "addMessage",
            role: "assistant",
            content: `Unknown command: /${command}\nType /help for available commands.`,
          });
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      this.postMessage({
        type: "addMessage",
        role: "assistant",
        content: `Error: ${message}`,
      });
    }
  }

  private _addToHistory(entry: { role: string; content: string }): void {
    this._chatHistory.push(entry);
    if (this._chatHistory.length > ChatViewProvider.MAX_HISTORY) {
      this._chatHistory.splice(0, this._chatHistory.length - ChatViewProvider.MAX_HISTORY);
    }
  }

  /** Return history with secrets scrubbed — for sending to providers. */
  private _getRedactedHistory(): Array<{ role: string; content: string }> {
    return this._chatHistory.map((m) => ({ role: m.role, content: redactSecrets(m.content) }));
  }

  // ── Free-text chat ─────────────────────────────────────────────────

  private async _handleChatMessage(text: string, provider: string = "ollama", model?: string): Promise<void> {
    this._addToHistory({ role: "user", content: text });

    // Build system prompt with fleet context
    const systemMsg = this._activeProjectId
      ? `You are a helpful AI assistant integrated into the Beethoven Fleet Control panel in VS Code. The user is working on project "${this._activeProjectName}". Help them with their questions. Keep responses concise.`
      : "You are a helpful AI assistant integrated into the Beethoven Fleet Control panel in VS Code. Help the user with their questions. Keep responses concise.";

    const messages = [
      { role: "system", content: systemMsg },
      ...this._getRedactedHistory().slice(-20), // Keep last 20 messages for context
    ];

    // Cancel any in-progress stream
    this._abortController?.abort();
    this._abortController = new AbortController();

    if (provider === "ollama") {
      await this._handleOllamaChat(messages, model);
    } else if (provider === "claude") {
      await this._handleClaudeSDK(text, model);
    } else {
      await this._handleCliChat(text, provider, model);
    }
  }

  /** Ollama streaming chat — direct fetch to local Ollama API. */
  private async _handleOllamaChat(
    messages: Array<{ role: string; content: string }>,
    model?: string
  ): Promise<void> {
    try {
      const resp = await fetch(`${this._ollamaUrl}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: model || this._ollamaModel,
          messages,
          stream: true,
        }),
        signal: this._abortController!.signal,
      });

      if (!resp.ok || !resp.body) {
        const errText = await resp.text().catch(() => "Unknown error");
        this.postMessage({
          type: "addMessage",
          role: "assistant",
          content: `Ollama error (${resp.status}): ${errText}`,
          provider: "ollama",
        });
        return;
      }

      // Create a placeholder message, then stream tokens into it
      this.postMessage({ type: "streamStart", provider: "ollama" });

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let fullResponse = "";
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) { break; }

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          if (!line.trim()) { continue; }
          try {
            const chunk = JSON.parse(line);
            if (chunk.message?.content) {
              fullResponse += chunk.message.content;
              this.postMessage({
                type: "streamToken",
                content: chunk.message.content,
              });
            }
          } catch {
            // Skip malformed JSON lines
          }
        }
      }

      this.postMessage({ type: "streamEnd", provider: "ollama" });
      this._addToHistory({ role: "assistant", content: fullResponse });
    } catch (err: unknown) {
      if (err instanceof Error && err.name === "AbortError") { return; }
      const message = err instanceof Error ? err.message : String(err);
      const isConnectionError = message.includes("ECONNREFUSED") || message.includes("fetch failed");
      this.postMessage({
        type: "addMessage",
        role: "assistant",
        content: isConnectionError
          ? `Could not connect to Ollama at ${this._ollamaUrl}.\n\nMake sure Ollama is running:\n  ollama serve\n\nOr update the URL in Settings > Beethoven > Ollama URL.`
          : `Chat error: ${message}`,
        provider: "ollama",
      });
    }
  }

  /** Claude SDK chat — uses claude-code-js with session continuity. Falls back to CLI on error. */
  private async _handleClaudeSDK(text: string, model?: string): Promise<void> {
    try {
      const systemPrompt = this._activeProjectId
        ? `You are a helpful AI assistant. The user is working on project "${this._activeProjectName}". Keep responses concise.`
        : "You are a helpful AI assistant. Keep responses concise.";

      const response = await this._claudeSDK.chat(
        {
          prompt: text,
          systemPrompt,
          ...(model ? { model } : {}),
        },
        this._claudeSessionId
      );

      if (response.success && response.message) {
        this._claudeSessionId = response.message.session_id;
        const content = response.message.result;
        const cost = response.message.cost_usd;

        this.postMessage({
          type: "addMessage",
          role: "assistant",
          content: content + (cost > 0 ? `\n\n_Cost: $${cost.toFixed(4)}_` : ""),
          provider: "claude",
        });
        this._addToHistory({ role: "assistant", content });
      } else {
        const errorMsg = response.error?.result ?? "Unknown SDK error";
        throw new Error(errorMsg);
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      const isNotFound = message.includes("ENOENT") || message.includes("not found") || message.includes("not recognized");

      if (isNotFound) {
        this.postMessage({
          type: "addMessage",
          role: "assistant",
          content: "Claude CLI not found.\n\nInstall it:\n  npm install -g @anthropic-ai/claude-code\n\nThen reload VS Code.",
          provider: "claude",
        });
        return;
      }

      // Fall back to raw CLI spawn
      vscode.window.showWarningMessage("Claude SDK error, falling back to CLI.");
      await this._handleCliChat(text, "claude", model);
    }
  }

  /** CLI-based chat — shells out to gemini/claude/codex CLIs directly. */
  private async _handleCliChat(text: string, provider: string, model?: string): Promise<void> {
    // Build prompt with conversation history
    const parts: string[] = [];

    const redacted = this._getRedactedHistory();
    if (redacted.length > 0) {
      const recent = redacted.slice(-20);
      const historyLines = recent.map(
        (m) => `[${m.role}]: ${m.content}`
      );
      parts.push("Previous conversation:\n" + historyLines.join("\n"));
    }

    parts.push(redacted.length > 0 ? `Current request:\n${text}` : text);
    const fullPrompt = parts.join("\n\n");

    // Build CLI command — prompt goes via stdin to avoid shell escaping issues
    let cmd: string;
    let args: string[];

    if (provider === "claude") {
      cmd = "claude";
      args = ["-p", "-", "--output-format", "text"];
      if (model) { args.push("--model", model); }
    } else if (provider === "codex") {
      cmd = "codex";
      args = ["exec", "--"];
      if (model) { args.splice(1, 0, "--model", model); }
    } else {
      // gemini (default)
      cmd = "gemini";
      args = ["-p", "-"];
      if (model) { args.push("-m", model); }
    }

    try {
      const result = await this._runCli(cmd, args, fullPrompt);
      this.postMessage({
        type: "addMessage",
        role: "assistant",
        content: result,
        provider: provider,
      });
      this._addToHistory({ role: "assistant", content: result });
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      const isNotFound = message.includes("ENOENT") || message.includes("not found") || message.includes("not recognized");
      this.postMessage({
        type: "addMessage",
        role: "assistant",
        content: isNotFound
          ? `The "${cmd}" CLI was not found.\n\nInstall it first:\n${provider === "gemini" ? "  npm install -g @google/gemini-cli" : provider === "claude" ? "  npm install -g @anthropic-ai/claude-code" : "  npm install -g @openai/codex"}\n\nThen reload VS Code.`
          : `${provider} error: ${message}`,
        provider: provider,
      });
    }
  }

  /** Run a CLI command and return stdout. Optionally pipes stdinData to avoid shell escaping. */
  private _runCli(cmd: string, args: string[], stdinData?: string): Promise<string> {
    return new Promise((resolve, reject) => {
      let stdout = "";
      let stderr = "";

      const proc = spawn(cmd, args, {
        shell: true, // Required on Windows for .cmd resolution
        timeout: 120_000,
      });

      if (stdinData !== undefined) {
        proc.stdin.write(stdinData);
        proc.stdin.end();
      }

      proc.stdout.on("data", (data: Buffer) => { stdout += data.toString(); });
      proc.stderr.on("data", (data: Buffer) => { stderr += data.toString(); });

      proc.on("error", (err: Error) => reject(err));

      proc.on("close", (code: number | null) => {
        if (code !== 0) {
          reject(new Error(stderr.trim() || `${cmd} exited with code ${code}`));
        } else {
          resolve(stdout.trim());
        }
      });

      // Allow abort
      if (this._abortController) {
        this._abortController.signal.addEventListener("abort", () => {
          proc.kill();
        });
      }
    });
  }

  // ── Webview HTML ───────────────────────────────────────────────────

  private _getHtmlForWebview(_webview: vscode.Webview): string {
    const nonce = getNonce();

    return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy"
    content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style nonce="${nonce}">
    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      display: flex;
      flex-direction: column;
      height: 100vh;
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size);
      color: var(--vscode-foreground);
      background: var(--vscode-sideBar-background);
    }

    /* ── Top bar ─────────────────────────────────── */
    .top-bar {
      display: flex;
      align-items: center;
      padding: 6px 10px;
      border-bottom: 1px solid var(--vscode-panel-border);
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      flex-shrink: 0;
    }
    .top-bar .project-name {
      font-weight: 600;
      color: var(--vscode-foreground);
      margin-right: 4px;
    }
    .top-bar .spacer {
      flex: 1;
    }
    .top-bar select {
      background: var(--vscode-dropdown-background);
      border: 1px solid var(--vscode-dropdown-border);
      color: var(--vscode-dropdown-foreground);
      font-size: 11px;
      padding: 2px 6px;
      border-radius: 3px;
      cursor: pointer;
      outline: none;
    }
    .top-bar select:focus {
      border-color: var(--vscode-focusBorder);
    }

    /* ── Message list ────────────────────────────── */
    .messages {
      flex: 1;
      overflow-y: auto;
      padding: 8px 10px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .msg {
      padding: 6px 8px;
      border-radius: 4px;
      white-space: pre-wrap;
      word-wrap: break-word;
      line-height: 1.4;
    }
    .msg.user {
      background: var(--vscode-textBlockQuote-background);
      align-self: flex-end;
      max-width: 90%;
    }
    .msg.assistant {
      align-self: flex-start;
      max-width: 90%;
    }
    .msg.system {
      font-style: italic;
      color: var(--vscode-descriptionForeground);
      font-size: 0.9em;
      align-self: center;
      text-align: center;
    }
    .msg .provider-badge {
      display: inline-block;
      font-size: 9px;
      padding: 1px 5px;
      border-radius: 3px;
      margin-bottom: 4px;
      background: var(--vscode-badge-background);
      color: var(--vscode-badge-foreground);
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.3px;
    }

    /* ── Input area ──────────────────────────────── */
    .input-area {
      display: flex;
      gap: 4px;
      padding: 8px 10px;
      border-top: 1px solid var(--vscode-panel-border);
      flex-shrink: 0;
    }
    .input-area textarea {
      flex: 1;
      resize: none;
      border: 1px solid var(--vscode-input-border);
      background: var(--vscode-input-background);
      color: var(--vscode-input-foreground);
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size);
      padding: 6px 8px;
      border-radius: 3px;
      min-height: 32px;
      max-height: 120px;
      outline: none;
    }
    .input-area textarea:focus {
      border-color: var(--vscode-focusBorder);
    }
    .input-area button {
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      border: none;
      border-radius: 3px;
      padding: 6px 12px;
      cursor: pointer;
      font-size: var(--vscode-font-size);
      flex-shrink: 0;
      align-self: flex-end;
    }
    .input-area button:hover {
      background: var(--vscode-button-hoverBackground);
    }
    .input-area button.stop-btn {
      background: var(--vscode-inputValidation-errorBackground, #5a1d1d);
      display: none;
    }
    .input-area button.stop-btn.visible {
      display: inline-block;
    }

    /* ── Thinking indicator ───────────────────────── */
    .thinking {
      display: none;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      font-size: 12px;
      color: var(--vscode-descriptionForeground);
      font-style: italic;
    }
    .thinking.visible { display: flex; }
    .thinking .dots::after {
      content: '';
      animation: dots 1.5s steps(4, end) infinite;
    }
    @keyframes dots {
      0% { content: ''; }
      25% { content: '.'; }
      50% { content: '..'; }
      75% { content: '...'; }
    }

    /* ── Markdown in messages ─────────────────────── */
    .msg code {
      background: var(--vscode-textCodeBlock-background);
      padding: 1px 4px;
      border-radius: 3px;
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 0.9em;
    }
    .msg pre {
      background: var(--vscode-textCodeBlock-background);
      padding: 8px;
      border-radius: 4px;
      overflow-x: auto;
      margin: 4px 0;
    }
    .msg pre code {
      background: none;
      padding: 0;
    }
    .msg strong { font-weight: 600; }
    .msg em { font-style: italic; }
    .msg a {
      color: var(--vscode-textLink-foreground);
      text-decoration: none;
    }
    .msg a:hover { text-decoration: underline; }
    .msg ul, .msg ol {
      margin: 4px 0 4px 16px;
    }

    /* ── Welcome message ─────────────────────────── */
    .welcome {
      padding: 16px;
      color: var(--vscode-descriptionForeground);
      text-align: center;
      line-height: 1.6;
    }
    .welcome h3 {
      color: var(--vscode-foreground);
      margin-bottom: 8px;
      font-size: 14px;
    }
    .welcome .hint {
      font-size: 11px;
      margin-top: 8px;
    }

    /* ── Usage bar ──────────────────────────────── */
    .usage-bar {
      display: flex;
      gap: 8px;
      padding: 3px 10px;
      font-size: 10px;
      color: var(--vscode-descriptionForeground);
      border-top: 1px solid var(--vscode-panel-border);
      flex-shrink: 0;
    }
    .usage-bar .usage-item {
      opacity: 0.7;
    }
    .usage-bar .usage-item.active {
      opacity: 1;
    }
  </style>
</head>
<body>
  <div class="top-bar">
    <span class="project-name" id="projectName">No project selected</span>
    <span class="spacer"></span>
    <select id="modelSelect">
      <optgroup label="Gemini">
        <option value="gemini:gemini-2.5-flash">Gemini Flash 2.5</option>
        <option value="gemini:gemini-2.5-pro">Gemini Pro 2.5</option>
        <option value="gemini:gemini-2.0-flash">Gemini Flash 2.0</option>
      </optgroup>
      <optgroup label="Claude">
        <option value="claude:haiku">Claude Haiku</option>
        <option value="claude:sonnet">Claude Sonnet</option>
        <option value="claude:opus">Claude Opus</option>
      </optgroup>
      <optgroup label="Codex">
        <option value="codex:gpt-4.1-mini">GPT-4.1 Mini</option>
        <option value="codex:gpt-4.1">GPT-4.1</option>
        <option value="codex:o3-mini">o3-mini</option>
      </optgroup>
      <optgroup label="Ollama (local)">
        <option value="ollama:qwen2.5-coder:14b">Qwen 2.5 Coder 14B</option>
        <option value="ollama:llama3.1:8b">Llama 3.1 8B</option>
      </optgroup>
    </select>
  </div>

  <div class="messages" id="messages">
    <div class="welcome" id="welcome">
      <h3>Beethoven Chat</h3>
      <div>Chat with AI using the provider dropdown above.</div>
      <div class="hint">Select a provider (Gemini, Claude, Codex) and start chatting.<br>Type <strong>/help</strong> for available commands.</div>
    </div>
  </div>

  <div class="thinking" id="thinking">
    <span>Thinking<span class="dots"></span></span>
  </div>

  <div class="usage-bar" id="usageBar"></div>

  <div class="input-area">
    <textarea id="input" rows="1" placeholder="Message or /command..."></textarea>
    <button class="stop-btn" id="stopBtn">Stop</button>
    <button id="sendBtn">Send</button>
  </div>

  <script nonce="${nonce}">
    (function() {
      const vscode = acquireVsCodeApi();
      const messagesEl = document.getElementById('messages');
      const inputEl = document.getElementById('input');
      const sendBtn = document.getElementById('sendBtn');
      const stopBtn = document.getElementById('stopBtn');
      const projectNameEl = document.getElementById('projectName');
      const thinkingEl = document.getElementById('thinking');
      const welcomeEl = document.getElementById('welcome');
      const modelSelect = document.getElementById('modelSelect');

      const PROVIDER_LABELS = {
        ollama: 'Ollama',
        gemini: 'Gemini',
        claude: 'Claude',
        codex: 'Codex'
      };

      function getSelection() {
        var val = modelSelect ? modelSelect.value : '';
        var idx = val.indexOf(':');
        if (idx < 0) { return { provider: 'gemini', model: val }; }
        return { provider: val.substring(0, idx), model: val.substring(idx + 1) };
      }

      if (modelSelect) {
        modelSelect.onchange = function() {
          vscode.postMessage({ type: 'providerChanged', value: modelSelect.value });
        };
      }

      const SLASH_COMMANDS = ['status', 'tasks', 'start', 'pause', 'refresh', 'help'];

      // Usage tracking
      const usageBarEl = document.getElementById('usageBar');
      const usageCounts = { ollama: 0, gemini: 0, claude: 0, codex: 0 };

      function updateUsageBar() {
        const parts = Object.entries(usageCounts)
          .filter(([, count]) => count > 0)
          .map(([provider, count]) => {
            const label = PROVIDER_LABELS[provider] || provider;
            return '<span class="usage-item active">' + label + ': ' + count + '</span>';
          });
        usageBarEl.innerHTML = parts.length ? parts.join('') : '';
      }

      function trackUsage(provider) {
        if (provider && provider in usageCounts) {
          usageCounts[provider]++;
          updateUsageBar();
        }
      }

      /** Scrub known secret patterns before display. */
      function redactForDisplay(text) {
        return text
          .replace(/orch_[0-9a-fA-F]{16,}/g, '[REDACTED:api-key]')
          .replace(/eyJ[A-Za-z0-9_-]{20,}\\.eyJ[A-Za-z0-9_-]{20,}\\.[A-Za-z0-9_-]{20,}/g, '[REDACTED:jwt]')
          .replace(/Bearer\\s+[A-Za-z0-9_.-]{20,}/gi, 'Bearer [REDACTED:token]');
      }

      /** Lightweight markdown to HTML — handles code blocks, inline code, bold, italic. */
      function renderMarkdown(text) {
        var html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        var tick = String.fromCharCode(96);
        // Fenced code blocks (triple backtick)
        var fenceRe = new RegExp(tick+tick+tick+'(\\w*)\\n([\\s\\S]*?)'+tick+tick+tick, 'g');
        html = html.replace(fenceRe, function(m, lang, code) {
          return '<pre><code>' + code.replace(/\n$/, '') + '</code></pre>';
        });
        // Inline code (single backtick)
        var inlineRe = new RegExp(tick+'([^'+tick+']+)'+tick, 'g');
        html = html.replace(inlineRe, '<code>$1</code>');
        // Bold
        html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        // Italic (single asterisk, but not inside bold)
        html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
        // Line breaks (but not inside pre blocks)
        var segments = html.split(/(<pre>[\s\S]*?<\/pre>)/g);
        html = segments.map(function(s) {
          return s.indexOf('<pre>') === 0 ? s : s.replace(/\n/g, '<br>');
        }).join('');
        return html;
      }

      function hideWelcome() {
        if (welcomeEl) { welcomeEl.style.display = 'none'; }
      }

      function setThinking(visible) {
        thinkingEl.classList.toggle('visible', visible);
        stopBtn.classList.toggle('visible', visible);
        sendBtn.style.display = visible ? 'none' : '';
        if (visible) { messagesEl.scrollTop = messagesEl.scrollHeight; }
      }

      function addMessage(role, content, provider) {
        hideWelcome();
        setThinking(false);
        const div = document.createElement('div');
        div.className = 'msg ' + role;
        if (role === 'assistant' && provider) {
          const badge = document.createElement('span');
          badge.className = 'provider-badge';
          badge.textContent = PROVIDER_LABELS[provider] || provider;
          div.appendChild(badge);
          div.appendChild(document.createElement('br'));
          trackUsage(provider);
        }
        if (role === 'assistant') {
          const contentSpan = document.createElement('span');
          contentSpan.innerHTML = renderMarkdown(redactForDisplay(content));
          div.appendChild(contentSpan);
        } else {
          div.appendChild(document.createTextNode(redactForDisplay(content)));
        }
        messagesEl.appendChild(div);
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }

      function send() {
        const text = inputEl.value.trim();
        if (!text) return;

        hideWelcome();
        addMessage('user', text);
        inputEl.value = '';
        autoResize();

        // Check for slash commands
        if (text.startsWith('/')) {
          const parts = text.slice(1).split(/\\s+/);
          const cmd = parts[0].toLowerCase();
          const args = parts.slice(1).join(' ');
          if (SLASH_COMMANDS.includes(cmd)) {
            vscode.postMessage({ type: 'slashCommand', command: cmd, args: args || undefined });
            return;
          }
        }

        setThinking(true);
        var sel = getSelection();
        vscode.postMessage({ type: 'sendMessage', text: text, provider: sel.provider, model: sel.model });
      }

      stopBtn.addEventListener('click', function() {
        vscode.postMessage({ type: 'stopGeneration' });
        setThinking(false);
      });

      function autoResize() {
        inputEl.style.height = 'auto';
        inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
      }

      sendBtn.addEventListener('click', send);

      inputEl.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          send();
        }
      });

      inputEl.addEventListener('input', autoResize);

      // Streaming state
      let streamDiv = null;
      let streamContentSpan = null;
      let streamFullText = '';

      // Messages from the extension
      window.addEventListener('message', function(event) {
        const msg = event.data;
        switch (msg.type) {
          case 'addMessage':
            addMessage(msg.role, msg.content, msg.provider);
            break;
          case 'setProject':
            projectNameEl.textContent = msg.name || 'No project selected';
            break;
          case 'streamStart': {
            hideWelcome();
            setThinking(false);
            streamDiv = document.createElement('div');
            streamDiv.className = 'msg assistant';
            streamFullText = '';
            if (msg.provider) {
              const badge = document.createElement('span');
              badge.className = 'provider-badge';
              badge.textContent = PROVIDER_LABELS[msg.provider] || msg.provider;
              streamDiv.appendChild(badge);
              streamDiv.appendChild(document.createElement('br'));
            }
            streamContentSpan = document.createElement('span');
            streamDiv.appendChild(streamContentSpan);
            messagesEl.appendChild(streamDiv);
            stopBtn.classList.add('visible');
            sendBtn.style.display = 'none';
            break;
          }
          case 'streamToken':
            if (streamDiv && streamContentSpan) {
              streamFullText += msg.content;
              streamContentSpan.innerHTML = renderMarkdown(redactForDisplay(streamFullText));
              messagesEl.scrollTop = messagesEl.scrollHeight;
            }
            break;
          case 'streamEnd':
            if (msg.provider) { trackUsage(msg.provider); }
            streamDiv = null;
            streamContentSpan = null;
            streamFullText = '';
            stopBtn.classList.remove('visible');
            sendBtn.style.display = '';
            break;
          case 'restoreSelection':
            if (msg.value && modelSelect) {
              modelSelect.value = msg.value;
            }
            break;
        }
      });

      // Signal ready so extension can send saved selection
      vscode.postMessage({ type: 'webviewReady' });
    })();
  </script>
</body>
</html>`;
  }
}

/** Generate a random nonce for Content-Security-Policy. */
function getNonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let nonce = "";
  for (let i = 0; i < 32; i++) {
    nonce += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return nonce;
}
