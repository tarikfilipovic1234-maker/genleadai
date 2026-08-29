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
    /** Stable machine-readable code, e.g. "rate_limited". Branch on this
     *  rather than on the message, which is free to be reworded. */
    readonly code: string = "unknown",
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
      "unreachable",
    );
  }

  if (!response.ok) {
    const { message, code } = await describeFailure(response);
    throw new ApiError(message, response.status, code);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

/**
 * Read the backend's error envelope.
 *
 * Every failure arrives as `{ error: { code, message, request_id } }` -
 * including validation failures, which the backend flattens from FastAPI's
 * nested loc/msg structure into readable text before sending. The `code` is
 * the stable part; the message is what gets shown.
 *
 * The fallbacks below cover responses that never reached the application: a
 * proxy's own 502 page, or a gateway timeout, neither of which knows about
 * this envelope.
 */
async function describeFailure(
  response: Response,
): Promise<{ message: string; code: string }> {
  try {
    const body = await response.json();
    if (typeof body?.error?.message === "string") {
      return {
        message: body.error.message,
        code: String(body.error.code ?? "unknown"),
      };
    }
    if (typeof body?.detail === "string") {
      return { message: body.detail, code: "unknown" };
    }
  } catch {
    /* not JSON - fall through to the status text */
  }
  return {
    message: `${response.status} ${response.statusText}`,
    code: "unknown",
  };
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
