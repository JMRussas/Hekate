//  Beethoven VSCode Extension - Chat WebviewView Provider
//
//  Renders an inline chat panel in the sidebar below the fleet tree.
//  Supports slash commands (/status, /tasks, /start, /pause) and
//  free-text chat routed to the orchestration API via BeethovenClient.
//
//  Depends on: ../api/client.ts (BeethovenClient)
//  Used by:    extension.ts

import * as vscode from "vscode";
import { BeethovenClient } from "../api/client";

export class ChatViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "beethovenChat";

  private _view?: vscode.WebviewView;
  private _activeProjectId?: string;
  private _activeProjectName?: string;

  private _client: BeethovenClient;

  constructor(
    private readonly _extensionUri: vscode.Uri,
    client: BeethovenClient
  ) {
    this._client = client;
  }

  public updateClient(client: BeethovenClient): void {
    this._client = client;
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

    webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);

    webviewView.webview.onDidReceiveMessage(async (msg) => {
      switch (msg.type) {
        case "sendMessage":
          await this._handleChatMessage(msg.text);
          break;
        case "slashCommand":
          await this._handleSlashCommand(msg.command, msg.args);
          break;
      }
    });
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
        default:
          this.postMessage({
            type: "addMessage",
            role: "assistant",
            content: `Unknown command: /${command}\nAvailable: /status, /tasks, /start, /pause`,
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

  // ── Free-text chat ─────────────────────────────────────────────────

  private async _handleChatMessage(text: string): Promise<void> {
    try {
      const response = await this._client.sendChatMessage(
        text,
        this._activeProjectId
      );
      this.postMessage({
        type: "addMessage",
        role: "assistant",
        content: response,
      });
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      this.postMessage({
        type: "addMessage",
        role: "assistant",
        content: `Error: ${message}`,
      });
    }
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
  </style>
</head>
<body>
  <div class="top-bar">
    <span class="project-name" id="projectName">No project selected</span>
  </div>

  <div class="messages" id="messages"></div>

  <div class="input-area">
    <textarea id="input" rows="1" placeholder="Message or /command..."></textarea>
    <button id="sendBtn">Send</button>
  </div>

  <script nonce="${nonce}">
    (function() {
      const vscode = acquireVsCodeApi();
      const messagesEl = document.getElementById('messages');
      const inputEl = document.getElementById('input');
      const sendBtn = document.getElementById('sendBtn');
      const projectNameEl = document.getElementById('projectName');

      const SLASH_COMMANDS = ['status', 'tasks', 'start', 'pause'];

      function addMessage(role, content) {
        const div = document.createElement('div');
        div.className = 'msg ' + role;
        div.textContent = content;
        messagesEl.appendChild(div);
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }

      function send() {
        const text = inputEl.value.trim();
        if (!text) return;

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

        vscode.postMessage({ type: 'sendMessage', text: text });
      }

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

      // Messages from the extension
      window.addEventListener('message', function(event) {
        const msg = event.data;
        switch (msg.type) {
          case 'addMessage':
            addMessage(msg.role, msg.content);
            break;
          case 'setProject':
            projectNameEl.textContent = msg.name || 'No project selected';
            break;
        }
      });
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
