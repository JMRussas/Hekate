//  Beethoven VSCode Extension - API Type Definitions
//
//  TypeScript interfaces matching the Orchestration REST API.
//
//  Depends on: (none)
//  Used by:    client.ts, extension.ts, fleetTree.ts, chatView.ts

export type ProjectStatus =
  | "draft"
  | "planning"
  | "ready"
  | "executing"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export type TaskStatus =
  | "pending"
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "blocked"
  | "cancelled"
  | "needs_review";

export interface Project {
  id: string;
  name: string;
  requirements: string;
  status: ProjectStatus;
  created_at: number;
  updated_at: number;
}

export interface Task {
  id: string;
  project_id: string;
  title: string;
  description: string;
  task_type: string;
  status: TaskStatus;
  model_tier: string;
  wave: number;
  phase: string | null;
  output_text: string | null;
  retry_count: number;
  created_at: number;
  updated_at: number;
}

export interface ProjectDetail extends Project {
  tasks: Task[];
  total_tasks: number;
  completed_tasks: number;
  failed_tasks: number;
  running_tasks: number;
}

export interface ServiceStatus {
  name: string;
  status: "available" | "unavailable" | "degraded";
  last_check: number;
}

export interface SSEEvent {
  event: string;
  data: Record<string, unknown>;
}

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
  timestamp?: number;
}
