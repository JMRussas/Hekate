//  Beethoven VSCode Extension - Fleet TreeView
//
//  TreeDataProvider for the sidebar tree. Shows projects (grouped by wave)
//  and agent/service statuses in a two-root hierarchy.
//
//  Depends on: ../api/types, ../api/client
//  Used by:    extension.ts

import * as vscode from "vscode";
import {
  ProjectDetail,
  Task,
  TaskStatus,
  ServiceStatus,
  ProjectStatus,
} from "../api/types";
import { BeethovenClient } from "../api/client";

// ---------------------------------------------------------------------------
//  Status icon maps
// ---------------------------------------------------------------------------

const TASK_STATUS_ICONS: Record<TaskStatus, string> = {
  completed: "\u2713",   // ✓
  running: "\u27F3",     // ⟳
  failed: "\u2717",      // ✗
  blocked: "\u2298",     // ⊘
  pending: "\u25CB",     // ○
  queued: "\u25CB",      // ○
  cancelled: "\u2717",   // ✗
  needs_review: "\u2049", // ⁉
};

const PROJECT_STATUS_ICONS: Record<ProjectStatus, string> = {
  draft: "\u25CB",       // ○
  planning: "\u25CB",    // ○
  ready: "\u25CB",       // ○
  executing: "\u27F3",   // ⟳
  paused: "\u23F8",      // ⏸
  completed: "\u2713",   // ✓
  failed: "\u2717",      // ✗
  cancelled: "\u2717",   // ✗
};

const SERVICE_STATUS_ICONS: Record<string, string> = {
  available: "\u2713",   // ✓
  unavailable: "\u2717", // ✗
  degraded: "\u26A0",   // ⚠
};

// ---------------------------------------------------------------------------
//  FleetItem — tree node
// ---------------------------------------------------------------------------

export type FleetItemType = "project" | "wave" | "task" | "agent" | "header";

export class FleetItem extends vscode.TreeItem {
  public readonly itemType: FleetItemType;
  public readonly projectId?: string;
  public readonly taskId?: string;

  constructor(
    label: string,
    itemType: FleetItemType,
    collapsible: vscode.TreeItemCollapsibleState,
    options?: {
      projectId?: string;
      taskId?: string;
      description?: string;
      contextValue?: string;
      icon?: string;
    },
  ) {
    super(label, collapsible);
    this.itemType = itemType;
    this.projectId = options?.projectId;
    this.taskId = options?.taskId;

    if (options?.description) {
      this.description = options.description;
    }
    if (options?.contextValue) {
      this.contextValue = options.contextValue;
    }
    if (options?.icon) {
      this.iconPath = new vscode.ThemeIcon(
        itemType === "agent" ? "circle-filled" : "symbol-event",
      );
      // Prefer inline text label over ThemeIcon so Unicode status shows
      this.label = `${options.icon} ${label}`;
      this.iconPath = undefined;
    }
  }
}

// ---------------------------------------------------------------------------
//  FleetTreeProvider
// ---------------------------------------------------------------------------

export class FleetTreeProvider implements vscode.TreeDataProvider<FleetItem> {
  // -- event emitter for tree refresh ------------------------------------
  private _onDidChangeTreeData = new vscode.EventEmitter<
    FleetItem | undefined | null | void
  >();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private autoRefreshTimer: ReturnType<typeof setInterval> | undefined;
  private cachedProjects: ProjectDetail[] = [];
  private cachedServices: ServiceStatus[] = [];
  private _connected = false;

  private client: BeethovenClient;

  constructor(client: BeethovenClient) {
    this.client = client;
  }

  private setConnected(value: boolean): void {
    this._connected = value;
    vscode.commands.executeCommand("setContext", "beethoven.connected", value);
  }

  updateClient(client: BeethovenClient): void {
    this.client = client;
  }

  // -- public API --------------------------------------------------------

  refresh(): void {
    this._onDidChangeTreeData.fire();
  }

  setAutoRefresh(ms: number): void {
    this.clearAutoRefresh();
    if (ms > 0) {
      this.autoRefreshTimer = setInterval(() => this.refresh(), ms);
    }
  }

  clearAutoRefresh(): void {
    if (this.autoRefreshTimer) {
      clearInterval(this.autoRefreshTimer);
      this.autoRefreshTimer = undefined;
    }
  }

  dispose(): void {
    this.clearAutoRefresh();
    this._onDidChangeTreeData.dispose();
  }

  // -- TreeDataProvider --------------------------------------------------

  getTreeItem(element: FleetItem): vscode.TreeItem {
    return element;
  }

  async getChildren(element?: FleetItem): Promise<FleetItem[]> {
    if (!element) {
      return this.getRootItems();
    }

    switch (element.itemType) {
      case "header":
        return element.label?.toString().startsWith("Projects")
          ? this.getProjectItems()
          : this.getAgentItems();
      case "project":
        return this.getWaveItems(element.projectId!);
      case "wave":
        return this.getTasksForWave(element.projectId!, element);
      default:
        return [];
    }
  }

  // -- root --------------------------------------------------------------

  private async getRootItems(): Promise<FleetItem[]> {
    // Fetch everything in one shot — probe + cache
    try {
      const [projects, services] = await Promise.all([
        this.client.listProjects(),
        this.client.getServices().catch(() => [] as ServiceStatus[]),
      ]);
      this.cachedProjects = projects;
      this.cachedServices = services;
      this.setConnected(true);
    } catch {
      this.setConnected(false);
      return [
        new FleetItem(
          "Not connected",
          "header",
          vscode.TreeItemCollapsibleState.None,
          { description: "Backend unavailable — chat works without it" },
        ),
      ];
    }

    return [
      new FleetItem(
        "Projects",
        "header",
        vscode.TreeItemCollapsibleState.Expanded,
        { contextValue: "header" },
      ),
      new FleetItem(
        "Agents",
        "header",
        vscode.TreeItemCollapsibleState.Expanded,
        { contextValue: "header" },
      ),
    ];
  }

  // -- projects ----------------------------------------------------------

  private getProjectItems(): FleetItem[] {
    if (this.cachedProjects.length === 0) {
      return [
        new FleetItem(
          "No projects",
          "header",
          vscode.TreeItemCollapsibleState.None,
        ),
      ];
    }

    return this.cachedProjects.map((p) => {
      const icon = PROJECT_STATUS_ICONS[p.status] ?? "\u25CB";
      const progress = `${p.completed_tasks}/${p.total_tasks} tasks`;
      const contextValue =
        p.status === "executing"
          ? "project-running"
          : p.status === "paused"
            ? "project-paused"
            : "project";

      return new FleetItem(
        p.name,
        "project",
        vscode.TreeItemCollapsibleState.Collapsed,
        {
          projectId: p.id,
          description: `[${p.status}] (${progress})`,
          contextValue,
          icon,
        },
      );
    });
  }

  // -- waves -------------------------------------------------------------

  private getWaveItems(projectId: string): FleetItem[] {
    const project = this.cachedProjects.find((p) => p.id === projectId);
    if (!project || !project.tasks || project.tasks.length === 0) {
      return [
        new FleetItem(
          "No tasks",
          "header",
          vscode.TreeItemCollapsibleState.None,
        ),
      ];
    }

    const waves = new Map<number, Task[]>();
    for (const task of project.tasks) {
      const w = task.wave ?? 0;
      if (!waves.has(w)) {
        waves.set(w, []);
      }
      waves.get(w)!.push(task);
    }

    const sorted = [...waves.keys()].sort((a, b) => a - b);
    return sorted.map((waveNum) => {
      const item = new FleetItem(
        `Wave ${waveNum}`,
        "wave",
        vscode.TreeItemCollapsibleState.Collapsed,
        { projectId, contextValue: "wave" },
      );
      // Stash wave number for child lookup
      (item as FleetItem & { waveNum: number }).waveNum = waveNum;
      return item;
    });
  }

  // -- tasks in a wave ---------------------------------------------------

  private getTasksForWave(
    projectId: string,
    waveItem: FleetItem,
  ): FleetItem[] {
    const project = this.cachedProjects.find((p) => p.id === projectId);
    if (!project) {
      return [];
    }

    const waveNum = (waveItem as FleetItem & { waveNum?: number }).waveNum ?? 0;
    const tasks = project.tasks.filter((t) => (t.wave ?? 0) === waveNum);

    return tasks.map((t) => {
      const icon = TASK_STATUS_ICONS[t.status] ?? "\u25CB";
      const contextValue = this.taskContextValue(t.status);

      return new FleetItem(
        t.title,
        "task",
        vscode.TreeItemCollapsibleState.None,
        {
          projectId,
          taskId: t.id,
          description: `[${t.status}]`,
          contextValue,
          icon,
        },
      );
    });
  }

  private taskContextValue(status: TaskStatus): string {
    switch (status) {
      case "failed":
        return "task-failed";
      case "running":
        return "task-running";
      default:
        return "task";
    }
  }

  // -- agents ------------------------------------------------------------

  private getAgentItems(): FleetItem[] {
    if (this.cachedServices.length === 0) {
      return [
        new FleetItem(
          "No agents",
          "header",
          vscode.TreeItemCollapsibleState.None,
        ),
      ];
    }

    return this.cachedServices.map((s) => {
      const icon = SERVICE_STATUS_ICONS[s.status] ?? "\u25CB";
      return new FleetItem(
        s.name,
        "agent",
        vscode.TreeItemCollapsibleState.None,
        {
          description: `[${s.status}]`,
          contextValue: "agent",
          icon,
        },
      );
    });
  }
}
