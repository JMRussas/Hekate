//  Beethoven VSCode Extension - Fleet TreeView
//
//  TreeDataProvider for the sidebar tree. Shows projects (grouped by wave)
//  and agent/service statuses in a two-root hierarchy.
//  Uses ThemeIcon + ThemeColor for colored status indicators.
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
//  Status → ThemeIcon mappings (icon id + color)
// ---------------------------------------------------------------------------

interface StatusIcon {
  icon: string;
  color: string;
}

const TASK_STATUS_ICONS: Record<TaskStatus, StatusIcon> = {
  completed:    { icon: "pass-filled",     color: "charts.green" },
  running:      { icon: "sync~spin",       color: "charts.blue" },
  failed:       { icon: "error",           color: "charts.red" },
  blocked:      { icon: "circle-slash",    color: "charts.yellow" },
  pending:      { icon: "circle-outline",  color: "foreground" },
  queued:       { icon: "circle-outline",  color: "foreground" },
  cancelled:    { icon: "close",           color: "disabledForeground" },
  needs_review: { icon: "warning",         color: "charts.yellow" },
};

const PROJECT_STATUS_ICONS: Record<ProjectStatus, StatusIcon> = {
  draft:     { icon: "circle-outline",  color: "foreground" },
  planning:  { icon: "circle-outline",  color: "foreground" },
  ready:     { icon: "circle-filled",   color: "charts.green" },
  executing: { icon: "sync~spin",       color: "charts.blue" },
  paused:    { icon: "debug-pause",     color: "charts.yellow" },
  completed: { icon: "pass-filled",     color: "charts.green" },
  failed:    { icon: "error",           color: "charts.red" },
  cancelled: { icon: "close",           color: "disabledForeground" },
};

const SERVICE_STATUS_ICONS: Record<string, StatusIcon> = {
  available:   { icon: "circle-filled",  color: "charts.green" },
  online:      { icon: "circle-filled",  color: "charts.green" },
  unavailable: { icon: "circle-slash",   color: "charts.red" },
  offline:     { icon: "circle-slash",   color: "charts.red" },
  degraded:    { icon: "warning",        color: "charts.yellow" },
};

const DEFAULT_ICON: StatusIcon = { icon: "circle-outline", color: "foreground" };

function statusThemeIcon(si: StatusIcon): vscode.ThemeIcon {
  return new vscode.ThemeIcon(si.icon, new vscode.ThemeColor(si.color));
}

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
      themeIcon?: vscode.ThemeIcon;
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
    if (options?.themeIcon) {
      this.iconPath = options.themeIcon;
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
          {
            description: "Backend unavailable — chat works without it",
            themeIcon: new vscode.ThemeIcon(
              "circle-slash",
              new vscode.ThemeColor("charts.red"),
            ),
          },
        ),
      ];
    }

    return [
      new FleetItem(
        "Projects",
        "header",
        vscode.TreeItemCollapsibleState.Expanded,
        {
          contextValue: "header",
          themeIcon: new vscode.ThemeIcon("project"),
        },
      ),
      new FleetItem(
        "Agents",
        "header",
        vscode.TreeItemCollapsibleState.Expanded,
        {
          contextValue: "header",
          themeIcon: new vscode.ThemeIcon("server-environment"),
        },
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
          {
            themeIcon: new vscode.ThemeIcon("info"),
          },
        ),
      ];
    }

    return this.cachedProjects.map((p) => {
      const si = PROJECT_STATUS_ICONS[p.status] ?? DEFAULT_ICON;
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
          description: `${p.status} · ${progress}`,
          contextValue,
          themeIcon: statusThemeIcon(si),
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
      const tasks = waves.get(waveNum)!;
      const completed = tasks.filter((t) => t.status === "completed").length;
      const failed = tasks.filter((t) => t.status === "failed").length;
      const running = tasks.filter((t) => t.status === "running").length;

      // Wave icon reflects aggregate state
      let waveIcon: StatusIcon;
      if (failed > 0) {
        waveIcon = { icon: "warning", color: "charts.red" };
      } else if (running > 0) {
        waveIcon = { icon: "sync~spin", color: "charts.blue" };
      } else if (completed === tasks.length) {
        waveIcon = { icon: "pass-filled", color: "charts.green" };
      } else {
        waveIcon = { icon: "circle-outline", color: "foreground" };
      }

      const item = new FleetItem(
        `Wave ${waveNum}`,
        "wave",
        vscode.TreeItemCollapsibleState.Collapsed,
        {
          projectId,
          contextValue: "wave",
          description: `${completed}/${tasks.length} done`,
          themeIcon: statusThemeIcon(waveIcon),
        },
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
      const si = TASK_STATUS_ICONS[t.status] ?? DEFAULT_ICON;
      const contextValue = this.taskContextValue(t.status);

      return new FleetItem(
        t.title,
        "task",
        vscode.TreeItemCollapsibleState.None,
        {
          projectId,
          taskId: t.id,
          description: t.status,
          contextValue,
          themeIcon: statusThemeIcon(si),
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
          {
            themeIcon: new vscode.ThemeIcon("info"),
          },
        ),
      ];
    }

    return this.cachedServices.map((s) => {
      const si = SERVICE_STATUS_ICONS[s.status] ?? DEFAULT_ICON;
      return new FleetItem(
        s.name,
        "agent",
        vscode.TreeItemCollapsibleState.None,
        {
          description: s.status,
          contextValue: "agent",
          themeIcon: statusThemeIcon(si),
        },
      );
    });
  }
}
