"use client";

/**
 * Subscribes to a task's server-sent events.
 *
 * Uses the browser's native EventSource rather than a fetch-based reader,
 * and that choice is the reason the backend keys its events on the
 * `run_events` primary key: EventSource automatically resends the last id it
 * saw as `Last-Event-ID` when the connection drops, so reconnect resumes
 * where it left off with no bookkeeping on this side. A fetch reader would
 * mean tracking and replaying that position by hand.
 *
 * The tradeoff is that EventSource cannot send custom headers - which is
 * exactly why the resume position travels in the standard one.
 */

import { useEffect, useState } from "react";
import { api } from "./api";
import type { AgentEvent, AgentEventType } from "./types";

const EVENT_TYPES: AgentEventType[] = [
  "run.started",
  "agent.message",
  "tool.called",
  "tool.result",
  "businesses.found",
  "lead.saved",
  "warning",
  "run.completed",
  "run.failed",
  "stream.end",
];

// Enough to show the shape of a run without letting a long one grow the DOM
// without bound. A 40-turn run emits roughly 90 events.
const MAX_RETAINED = 400;

export type StreamState = "idle" | "open" | "closed" | "error";

interface Snapshot {
  taskId: string | null;
  events: AgentEvent[];
  state: StreamState;
  finished: boolean;
}

const EMPTY: Snapshot = {
  taskId: null,
  events: [],
  state: "idle",
  finished: false,
};

export interface TaskStream {
  events: AgentEvent[];
  state: StreamState;
  /** Leads saved so far, so the table can refetch exactly when it should. */
  leadCount: number;
  finished: boolean;
}

export function useTaskStream(taskId: string | null): TaskStream {
  const [snapshot, setSnapshot] = useState<Snapshot>(EMPTY);

  // Everything about a stream belongs to one task, so the task id is stored
  // alongside it and the whole snapshot is replaced when the id changes.
  // This is React's documented way to reset state when an input changes -
  // adjusting during render rather than clearing in an effect, which would
  // render one frame of the previous run's transcript under the new task.
  if (snapshot.taskId !== taskId) {
    setSnapshot({ ...EMPTY, taskId, state: taskId ? "open" : "idle" });
  }

  useEffect(() => {
    if (!taskId) return;

    const source = new EventSource(api.streamUrl(taskId));

    const append = (type: AgentEventType, raw: MessageEvent) => {
      let payload: Record<string, unknown> = {};
      try {
        payload = JSON.parse(raw.data);
      } catch {
        payload = { raw: raw.data };
      }

      setSnapshot((current) => {
        // A late message from a previous task must not land in this one's
        // transcript; the id check makes that impossible.
        if (current.taskId !== taskId) return current;

        const events = [
          ...current.events,
          {
            id: raw.lastEventId ? Number(raw.lastEventId) : null,
            type,
            payload,
            receivedAt: Date.now(),
          },
        ];

        const ended = type === "stream.end";
        return {
          ...current,
          events: events.length > MAX_RETAINED ? events.slice(-MAX_RETAINED) : events,
          // The backend closes the bus only after the final task status is
          // committed, so this marker is the point at which refetching the
          // task returns "completed" rather than racing the write.
          finished: current.finished || ended,
          state: ended ? "closed" : current.state,
        };
      });

      if (type === "stream.end") source.close();
    };

    const listeners = EVENT_TYPES.map((type) => {
      const handler = (raw: Event) => append(type, raw as MessageEvent);
      source.addEventListener(type, handler);
      return { type, handler };
    });

    source.onerror = () => {
      // EventSource reconnects on its own, so an error mid-run is usually
      // transient. Only treat it as terminal once the socket is actually
      // closed, or a brief network blip would look like a failed run.
      if (source.readyState !== EventSource.CLOSED) return;
      setSnapshot((current) =>
        current.taskId === taskId && !current.finished
          ? { ...current, state: "error" }
          : current,
      );
    };

    return () => {
      for (const { type, handler } of listeners) {
        source.removeEventListener(type, handler);
      }
      source.close();
    };
  }, [taskId]);

  return {
    events: snapshot.events,
    state: snapshot.state,
    finished: snapshot.finished,
    leadCount: snapshot.events.filter((e) => e.type === "lead.saved").length,
  };
}
