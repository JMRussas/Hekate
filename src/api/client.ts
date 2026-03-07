//  Beethoven VSCode Extension - API Client
//
//  HTTP client for the Orchestration REST API. Uses built-in fetch()
//  with no external dependencies. Includes SSE streaming with
//  auto-reconnect for project event subscriptions.
//
//  Depends on: types.ts
//  Used by:    extension.ts, fleetTree.ts, chatView.ts

import type {
  Project,
  ProjectDetail,
  Task,
  ServiceStatus,
  SSEEvent,
  ChatMessage,
} from "./types";

const SSE_RECONNECT_DELAY_MS = 3000;
const SSE_MAX_RETRIES = 5;

export class BeethovenClient {
  private readonly apiUrl: string;
  private readonly apiKey: string;
  private _connected = false;

  constructor(apiUrl: string, apiKey?: string) {
    // Strip trailing slash for consistent path joining
    this.apiUrl = apiUrl.replace(/\/+$/, "");
    this.apiKey = apiKey ?? "";
  }

  isConnected(): boolean {
    return this._connected;
  }

  // ── Private helpers ──────────────────────────────────────────────

  private async _fetch<T>(path: string, options?: RequestInit): Promise<T> {
    const url = `${this.apiUrl}${path}`;
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {}),
      ...(options?.headers as Record<string, string> | undefined),
    };

    const response = await fetch(url, {
      ...options,
      headers,
    });

    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(
        `Beethoven API ${response.status}: ${response.statusText}${body ? ` — ${body}` : ""}`
      );
    }

    // 204 No Content — nothing to parse
    if (response.status === 204) {
      return undefined as T;
    }

    this._connected = true;
    return (await response.json()) as T;
  }

  private async _post<T>(path: string, body?: unknown): Promise<T> {
    return this._fetch<T>(path, {
      method: "POST",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  }

  // ── Projects ─────────────────────────────────────────────────────

  async getProjects(): Promise<Project[]> {
    return this._fetch<Project[]>("/api/projects");
  }

  async listProjects(): Promise<ProjectDetail[]> {
    const projects = await this.getProjects();
    const details = await Promise.all(
      projects.map((p) => this.getProject(p.id).catch(() => ({
        ...p,
        tasks: [],
        total_tasks: 0,
        completed_tasks: 0,
        failed_tasks: 0,
        running_tasks: 0,
      } as ProjectDetail)))
    );
    return details;
  }

  async getProject(id: string): Promise<ProjectDetail> {
    return this._fetch<ProjectDetail>(`/api/projects/${encodeURIComponent(id)}`);
  }

  async getRunningTaskCount(): Promise<number> {
    try {
      const projects = await this.getProjects();
      const details = await Promise.all(
        projects
          .filter((p) => p.status === "executing")
          .map((p) => this.getProject(p.id).catch(() => null))
      );
      return details.reduce((sum, d) => sum + (d?.running_tasks ?? 0), 0);
    } catch {
      return 0;
    }
  }

  async getFleetStatus(): Promise<string> {
    try {
      const projects = await this.getProjects();
      if (projects.length === 0) {
        return "No projects.";
      }
      return projects.map((p) => `${p.name}: ${p.status}`).join("\n");
    } catch (err) {
      return `Error: ${err instanceof Error ? err.message : String(err)}`;
    }
  }

  async startProject(id: string): Promise<void> {
    await this._post<void>(`/api/projects/${encodeURIComponent(id)}/start`);
  }

  async pauseProject(id: string): Promise<void> {
    await this._post<void>(`/api/projects/${encodeURIComponent(id)}/pause`);
  }

  // ── Tasks ────────────────────────────────────────────────────────

  async getTask(id: string): Promise<Task> {
    return this._fetch<Task>(`/api/tasks/${encodeURIComponent(id)}`);
  }

  async retryTask(id: string): Promise<void> {
    await this._post<void>(`/api/tasks/${encodeURIComponent(id)}/retry`);
  }

  async cancelTask(id: string): Promise<void> {
    await this._post<void>(`/api/tasks/${encodeURIComponent(id)}/cancel`);
  }

  // ── Services ─────────────────────────────────────────────────────

  async getServices(): Promise<ServiceStatus[]> {
    return this._fetch<ServiceStatus[]>("/api/services");
  }

  // ── Chat ─────────────────────────────────────────────────────────

  async sendChatMessage(message: string, projectId?: string): Promise<string> {
    const result = await this._post<ChatMessage>("/api/internal/chat", {
      project_id: projectId ?? "",
      message,
    });
    return result.content;
  }

  // ── SSE Streaming ────────────────────────────────────────────────

  /**
   * Subscribe to real-time project events via SSE.
   *
   * Fetches a short-lived token via POST, then opens an SSE stream.
   * Auto-reconnects on error with exponential backoff (3s base, max 5 retries).
   *
   * @returns AbortController — call .abort() to disconnect.
   */
  subscribeProject(
    projectId: string,
    onEvent: (event: SSEEvent) => void,
    onError?: (error: Error) => void
  ): AbortController {
    const controller = new AbortController();
    const encodedId = encodeURIComponent(projectId);

    const connect = async (retryCount: number): Promise<void> => {
      if (controller.signal.aborted) {
        return;
      }

      try {
        // Obtain SSE token
        const tokenResponse = await this._post<{ token: string }>(
          `/api/events/${encodedId}/token`
        );

        if (controller.signal.aborted) {
          return;
        }

        // Open SSE stream
        const sseUrl = `${this.apiUrl}/api/events/${encodedId}?token=${encodeURIComponent(tokenResponse.token)}`;
        const response = await fetch(sseUrl, {
          headers: this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {},
          signal: controller.signal,
        });

        if (!response.ok) {
          throw new Error(`SSE connect failed: ${response.status} ${response.statusText}`);
        }

        if (!response.body) {
          throw new Error("SSE response has no body");
        }

        // Reset retry count on successful connection
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let currentEvent = "message";
        let currentData = "";

        // eslint-disable-next-line no-constant-condition
        while (true) {
          const { done, value } = await reader.read();
          if (done || controller.signal.aborted) {
            break;
          }

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          // Keep the last partial line in the buffer
          buffer = lines.pop() ?? "";

          for (const line of lines) {
            if (line.startsWith("event:")) {
              currentEvent = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              currentData += line.slice(5).trim();
            } else if (line === "") {
              // Empty line = end of SSE message
              if (currentData) {
                try {
                  const parsed = JSON.parse(currentData) as Record<string, unknown>;
                  onEvent({ event: currentEvent, data: parsed });
                } catch {
                  // Non-JSON data — pass as raw string
                  onEvent({ event: currentEvent, data: { raw: currentData } });
                }
              }
              currentEvent = "message";
              currentData = "";
            }
          }
        }

        // Stream ended cleanly — reconnect unless aborted
        if (!controller.signal.aborted) {
          await connect(0);
        }
      } catch (err: unknown) {
        if (controller.signal.aborted) {
          return;
        }

        const error = err instanceof Error ? err : new Error(String(err));

        if (retryCount >= SSE_MAX_RETRIES) {
          onError?.(new Error(`SSE max retries (${SSE_MAX_RETRIES}) exceeded: ${error.message}`));
          return;
        }

        onError?.(error);

        // Wait before reconnecting
        await new Promise((resolve) => setTimeout(resolve, SSE_RECONNECT_DELAY_MS));

        if (!controller.signal.aborted) {
          await connect(retryCount + 1);
        }
      }
    };

    // Start connection asynchronously
    connect(0).catch((err) => {
      onError?.(err instanceof Error ? err : new Error(String(err)));
    });

    return controller;
  }
}
