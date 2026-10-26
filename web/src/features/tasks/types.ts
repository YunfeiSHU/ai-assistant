export type TaskStatus =
  | "PENDING"
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELED";

export interface TaskError {
  code: string;
  message: string;
  detail: Record<string, unknown>;
  at: string;
}

export interface Task {
  id: string;
  type: string;
  status: TaskStatus;
  resource_type: string;
  resource_id: string;
  progress: number;
  cancel_requested: boolean;
  stage: string | null;
  chunks_total: number;
  chunks_done: number;
  retry_count: number;
  max_retries: number;
  error: TaskError | null;
  cancelable: boolean;
  retryable: boolean;
  created_at: string;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
}

export interface TaskFilters {
  status?: TaskStatus;
  type?: string;
  resource_id?: string;
  limit?: number;
  cursor?: string | null;
}
