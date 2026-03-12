// Beethoven Fleet Control - Status Bar
//
// Creates and updates the status bar item showing fleet connection state
// and running task count. Uses VSCode theme colors for visual feedback.
//
// Depends on: (none)
// Used by:    extension.ts

import * as vscode from "vscode";

/**
 * Creates the Beethoven status bar item on the left side.
 */
export function createStatusBar(): vscode.StatusBarItem {
  const item = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    100
  );
  item.text = "$(hubot) Beethoven";
  item.tooltip = "Beethoven Fleet Control";
  item.command = "beethoven.refresh";
  item.show();
  return item;
}

/**
 * Updates the status bar item based on connection state and running task count.
 *
 * States:
 *   Disconnected — red background, circle-slash icon
 *   Tasks running — blue text, spinning sync icon + count
 *   Connected idle — green text, check icon
 */
export function updateStatusBar(
  item: vscode.StatusBarItem,
  connected: boolean,
  runningCount: number
): void {
  if (!connected) {
    item.text = "$(circle-slash) Beethoven — Offline";
    item.tooltip = "Beethoven Fleet Control — Disconnected";
    item.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
    item.color = undefined;
  } else if (runningCount > 0) {
    item.text = `$(sync~spin) Beethoven — ${runningCount} task${runningCount === 1 ? "" : "s"}`;
    item.tooltip = `Beethoven Fleet Control — ${runningCount} task${runningCount === 1 ? "" : "s"} running`;
    item.backgroundColor = undefined;
    item.color = new vscode.ThemeColor("charts.blue");
  } else {
    item.text = "$(check) Beethoven — Idle";
    item.tooltip = "Beethoven Fleet Control — Connected";
    item.backgroundColor = undefined;
    item.color = new vscode.ThemeColor("charts.green");
  }
}
