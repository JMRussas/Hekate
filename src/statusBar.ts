// Beethoven Fleet Control - Status Bar
//
// Creates and updates the status bar item showing fleet connection state
// and running task count.
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
 */
export function updateStatusBar(
  item: vscode.StatusBarItem,
  connected: boolean,
  runningCount: number
): void {
  if (!connected) {
    item.text = "$(hubot) Beethoven $(circle-slash)";
    item.tooltip = "Beethoven Fleet Control - Disconnected";
  } else if (runningCount > 0) {
    item.text = `$(hubot) Beethoven $(sync~spin) ${runningCount}`;
    item.tooltip = `Beethoven Fleet Control - ${runningCount} task${runningCount === 1 ? "" : "s"} running`;
  } else {
    item.text = "$(hubot) Beethoven $(check)";
    item.tooltip = "Beethoven Fleet Control - Idle";
  }
}
