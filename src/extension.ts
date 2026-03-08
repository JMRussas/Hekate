// Beethoven Fleet Control - Extension Entry Point
//
// Registers commands, creates client, wires tree + chat providers.
//
// Depends on: api/client.ts, views/fleetTree.ts, views/chatView.ts, statusBar.ts
// Used by:    VSCode (activation)

import * as vscode from "vscode";
import { BeethovenClient } from "./api/client";
import { FleetTreeProvider } from "./views/fleetTree";
import { ChatViewProvider } from "./views/chatView";
import { createStatusBar, updateStatusBar } from "./statusBar";

let refreshInterval: ReturnType<typeof setInterval> | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const config = vscode.workspace.getConfiguration("beethoven");
  const apiUrl = config.get<string>("apiUrl", "http://localhost:5200");
  const autoConnect = config.get<boolean>("autoConnect", true);

  const secrets = context.secrets;
  let apiKey = await secrets.get("beethoven.apiKey")
    || config.get<string>("apiKey", "")
    || process.env.BEETHOVEN_API_KEY;

  const ollamaUrl = config.get<string>("ollamaUrl", "http://localhost:11434");
  const ollamaModel = config.get<string>("ollamaModel", "qwen2.5-coder:14b");

  let client = new BeethovenClient(apiUrl, apiKey ?? undefined);

  // Tree view
  const treeProvider = new FleetTreeProvider(client);
  vscode.window.registerTreeDataProvider("beethovenTree", treeProvider);

  // Chat webview
  const chatProvider = new ChatViewProvider(context.extensionUri, client, ollamaUrl, ollamaModel, context.globalState);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("beethovenChat", chatProvider)
  );

  // Status bar
  const statusBarItem = createStatusBar();
  context.subscriptions.push(statusBarItem);

  // --- Commands ---

  context.subscriptions.push(
    vscode.commands.registerCommand("beethoven.refresh", () => {
      treeProvider.refresh();
      updateStatusBar(statusBarItem, client.isConnected(), 0);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("beethoven.selectProject", (item: { projectId?: string; label?: string | vscode.TreeItemLabel }) => {
      if (item.projectId) {
        const name = typeof item.label === "string" ? item.label : item.projectId;
        chatProvider.setActiveProject(item.projectId, name);
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("beethoven.startProject", async (item: { projectId?: string }) => {
      if (item.projectId) {
        await client.startProject(item.projectId);
        treeProvider.refresh();
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("beethoven.pauseProject", async (item: { projectId?: string }) => {
      if (item.projectId) {
        await client.pauseProject(item.projectId);
        treeProvider.refresh();
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("beethoven.retryTask", async (item: { taskId?: string }) => {
      if (item.taskId) {
        await client.retryTask(item.taskId);
        treeProvider.refresh();
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("beethoven.cancelTask", async (item: { taskId?: string }) => {
      if (item.taskId) {
        await client.cancelTask(item.taskId);
        treeProvider.refresh();
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("beethoven.connect", async () => {
      const key = await vscode.window.showInputBox({
        prompt: "Enter Beethoven API key",
        password: true,
        ignoreFocusOut: true,
      });
      if (key !== undefined) {
        await secrets.store("beethoven.apiKey", key);
        apiKey = key;
        client = new BeethovenClient(apiUrl, apiKey ?? undefined);
        treeProvider.updateClient(client);
        chatProvider.updateClient(client);
        treeProvider.refresh();
        const runningCount = await client.getRunningTaskCount();
        updateStatusBar(statusBarItem, client.isConnected(), runningCount);
        vscode.window.showInformationMessage("Beethoven: API key updated.");
      }
    })
  );

  // --- Auto-refresh ---

  if (autoConnect) {
    // Initial connection attempt
    client.getRunningTaskCount().then((count) => {
      updateStatusBar(statusBarItem, client.isConnected(), count);
    }).catch(() => {
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
