/**
 * Shapes returned by the backend.
 *
 * Written by hand rather than generated from the OpenAPI schema: the set is
 * small, and the comments here record what the fields *mean* - particularly
 * the provenance states, which a generator would reduce to `string`.
 */

/**
 * How a value came to be known.
 *
 * The distinction between `inferred` and `unverified` is the product. The
 * first is the agent's judgement from evidence it saw; the second means it
 * looked and could not confirm, and carries no value at all. Rendering them
 * identically would undo the whole system.
 */
export type Provenance = "verified" | "inferred" | "unverified";

export interface Fact {
  value: string | number | boolean | null;
  provenance: Provenance;
  source_url: string | null;
  evidence: string | null;
}

export interface ScoreContribution {
  rule: string;
  points: number;
  reason: string;
}

export interface Source {
  url: string;
  kind: "osm" | "website" | "web_search" | "social";
  title: string | null;
  excerpt: string | null;
  fetched_at: string;
}

export interface Lead {
  id: string;
  task_id: string;
  name: string;
  category: string | null;
  score: number;
  score_breakdown: ScoreContribution[];
  qualification_reason: string | null;
  sales_angle: string | null;
  outreach_message: string | null;
  provenance_summary: Record<Provenance, number>;
  facts: Record<string, Fact>;
  sources?: Source[];
  created_at: string;
}

export type TaskStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface Task {
  id: string;
  prompt: string;
  status: TaskStatus;
  target_count: number;
  lead_count: number;
  error: string | null;
  created_at: string;
  running?: boolean;
}

export interface CreatedTask {
  id: string;
  status: TaskStatus;
  stream: string;
}

export interface BackendConfig {
  runtime: "sdk" | "replay" | "manual";
  live_runs_enabled: boolean;
  environment: string;
}

/** Event types the stream emits. Mirrors app/agent/runtime.py. */
export type AgentEventType =
  | "run.started"
  | "agent.message"
  | "tool.called"
  | "tool.result"
  | "businesses.found"
  | "lead.saved"
  | "warning"
  | "run.completed"
  | "run.failed"
  | "stream.end";

export interface AgentEvent {
  id: number | null;
  type: AgentEventType;
  payload: Record<string, unknown>;
  receivedAt: number;
}
