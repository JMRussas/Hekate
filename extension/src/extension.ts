// Hekate Fleet Control - Extension Entry Point
//
// Registers commands, creates client, wires tree + chat providers.
//
// Depends on: api/client.ts, views/fleetTree.ts, views/chatView.ts, statusBar.ts
// Used by:    VSCode (activation)

import * as vscode from "vscode";
import { HekateClient } from "./api/client";
import { FleetTreeProvider } from "./views/fleetTree";
import { ChatViewProvider } from "./views/chatView";
import { createStatusBar, updateStatusBar } from "./statusBar";

let refreshInterval: ReturnType<typeof setInterval> | undefined;

const log = vscode.window.createOutputChannel("Hekate Fleet", { log: true });

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const config = vscode.workspace.getConfiguration("hekate");
  const apiUrl = config.get<string>("apiUrl", "http://localhost:5200");
  const autoConnect = config.get<boolean>("autoConnect", true);

  const secrets = context.secrets;
  const secretKey = await secrets.get("hekate.apiKey");
  const configKey = config.get<string>("apiKey", "");
  const envKey = process.env.HEKATE_API_KEY;
  let apiKey = secretKey || configKey || envKey;

  log.info(`API URL: ${apiUrl}`);
  log.info(`API key source: ${secretKey ? "secrets" : configKey ? "settings" : envKey ? "env" : "NONE"}`);
  log.info(`API key present: ${!!apiKey} (${apiKey ? apiKey.slice(0, 12) + "..." : "empty"})`);
  log.info(`Auto-connect: ${autoConnect}`);

  const ollamaUrl = config.get<string>("ollamaUrl", "http://localhost:11434");
  const ollamaModel = config.get<string>("ollamaModel", "qwen2.5-coder:14b");

  let client = new HekateClient(apiUrl, apiKey ?? undefined);

  // Tree view
  const treeProvider = new FleetTreeProvider(client);
  vscode.window.registerTreeDataProvider("hekateTree", treeProvider);

  // Chat webview
  const chatProvider = new ChatViewProvider(context.extensionUri, client, ollamaUrl, ollamaModel, context.globalState);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("hekateChat", chatProvider)
  );

  // Status bar
  const statusBarItem = createStatusBar();
  context.subscriptions.push(statusBarItem);

  // --- Commands ---

  context.subscriptions.push(
    vscode.commands.registerCommand("hekate.refresh", () => {
      treeProvider.refresh();
      updateStatusBar(statusBarItem, client.isConnected(), 0);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("hekate.selectProject", (item: { projectId?: string; label?: string | vscode.TreeItemLabel }) => {
      if (item.projectId) {
        const name = typeof item.label === "string" ? item.label : item.projectId;
        chatProvider.setActiveProject(item.projectId, name);
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("hekate.startProject", async (item: { projectId?: string }) => {
      if (item.projectId) {
        await client.startProject(item.projectId);
        treeProvider.refresh();
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("hekate.pauseProject", async (item: { projectId?: string }) => {
      if (item.projectId) {
        await client.pauseProject(item.projectId);
        treeProvider.refresh();
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("hekate.retryTask", async (item: { taskId?: string }) => {
      if (item.taskId) {
        await client.retryTask(item.taskId);
        treeProvider.refresh();
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("hekate.cancelTask", async (item: { taskId?: string }) => {
      if (item.taskId) {
        await client.cancelTask(item.taskId);
        treeProvider.refresh();
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("hekate.connect", async () => {
      const key = await vscode.window.showInputBox({
        prompt: "Enter Hekate API key",
        password: true,
        ignoreFocusOut: true,
      });
      if (key !== undefined) {
        await secrets.store("hekate.apiKey", key);
        apiKey = key;
        client = new HekateClient(apiUrl, apiKey ?? undefined);
        treeProvider.updateClient(client);
        chatProvider.updateClient(client);
        treeProvider.refresh();
        const runningCount = await client.getRunningTaskCount();
        updateStatusBar(statusBarItem, client.isConnected(), runningCount);
        vscode.window.showInformationMessage("Hekate: API key updated.");
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("hekate.diagnose", async () => {
      const lines = [
        `API URL: ${apiUrl}`,
        `API key source: ${secretKey ? "secrets" : configKey ? "settings" : envKey ? "env" : "NONE"}`,
        `API key: ${apiKey ? apiKey.slice(0, 12) + "..." : "(empty)"}`,
        `Connected: ${client.isConnected()}`,
        `Auto-connect: ${autoConnect}`,
      ];
      try {
        const projects = await client.getProjects();
        lines.push(`API test: OK (${projects.length} projects)`);
      } catch (err) {
        lines.push(`API test: FAILED — ${err instanceof Error ? err.message : String(err)}`);
      }
      const msg = lines.join("\n");
      log.info(msg);
      vscode.window.showInformationMessage(msg, { modal: true });
    })
  );

  // --- Auto-refresh ---

  if (autoConnect) {
    // Initial connection attempt
    client.getRunningTaskCount().then((count) => {
      log.info(`Initial connect: OK (${count} running)`);
      updateStatusBar(statusBarItem, client.isConnected(), count);
    }).catch((err) => {
      log.error(`Initial connect failed: ${err instanceof Error ? err.message : String(err)}`);
      updateStatusBar(statusBarItem, false, 0);
    });

    refreshInterval = setInterval(async () => {
      try {
        const count = await client.getRunningTaskCount();
        updateStatusBar(statusBarItem, client.isConnected(), count);
      } catch {
        updateStatusBar(statusBarItem, false, 0);
      }
    }, 30_000);

    context.subscriptions.push({
      dispose: () => {
        if (refreshInterval) {
          clearInterval(refreshInterval);
          refreshInterval = undefined;
        }
      },
    });
  }
}

export function deactivate(): void {
  if (refreshInterval) {
    clearInterval(refreshInterval);
    refreshInterval = undefined;
  }
}
