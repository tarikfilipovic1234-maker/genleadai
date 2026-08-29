/**
 * Backend client.
 *
 * Plain fetch wrappers, with SWR layered on top in the components that read
 * them. Fetching in an effect and storing the result in local state was the
 * obvious first approach and is the one React now lints against: it triggers
 * cascading renders, and two runs started in quick succession can resolve out
 * of order and leave the older result on screen. SWR owns that state instead,
 * deduplicates in-flight requests, and keys results so a stale response
 * cannot overwrite a newer one.
 *
 * Revalidation stays manual: the SSE stream already says exactly when new
 * data exists, so polling would be both slower to appear and busier when
 * nothing is happening.
 */

import type { BackendConfig, CreatedTask, Lead, Task } from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    // A network failure and a 500 need different words: one means the
    // backend is not running, the other means it is and something broke.
    throw new ApiError(
      `Cannot reach the backend at ${API_BASE}. Is it running?`,
      0,
    );
  }

  if (!response.ok) {
    throw new ApiError(await describeFailure(response), response.status);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

/** Turn FastAPI's validation envelope into something a person can act on. */
async function describeFailure(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (Array.isArray(body?.detail)) {
      return body.detail
        .map((d: { loc?: string[]; msg?: string }) => {
          const field = d.loc?.filter((p) => p !== "body").join(".");
          return field ? `${field}: ${d.msg}` : d.msg;
        })
        .join("; ");
    }
    if (typeof body?.detail === "string") return body.detail;
  } catch {
    /* fall through to the status text */
  }
  return `${response.status} ${response.statusText}`;
}

export const api = {
  config: () => request<BackendConfig>("/api/config"),

  createTask: (prompt: string, targetCount: number, scoringProfile?: string) =>
    request<CreatedTask>("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        prompt,
        target_count: targetCount,
        scoring_profile: scoringProfile ?? null,
      }),
    }),

  listTasks: () => request<Task[]>("/api/tasks"),

  getTask: (taskId: string) => request<Task>(`/api/tasks/${taskId}`),

  deleteTask: (taskId: string) =>
    request<void>(`/api/tasks/${taskId}`, { method: "DELETE" }),

  listLeads: (params: {
    taskId?: string;
    minScore?: number;
    search?: string;
    order?: "score" | "name" | "created";
  } = {}) => {
    const query = new URLSearchParams();
    if (params.taskId) query.set("task_id", params.taskId);
    if (params.minScore) query.set("min_score", String(params.minScore));
    if (params.search) query.set("search", params.search);
    if (params.order) query.set("order", params.order);
    return request<Lead[]>(`/api/leads?${query}`);
  },

  getLead: (leadId: string) => request<Lead>(`/api/leads/${leadId}`),

  deleteLead: (leadId: string) =>
    request<void>(`/api/leads/${leadId}`, { method: "DELETE" }),

  csvUrl: (taskId?: string) =>
    `${API_BASE}/api/leads.csv${taskId ? `?task_id=${taskId}` : ""}`,

  streamUrl: (taskId: string) => `${API_BASE}/api/tasks/${taskId}/stream`,
};
