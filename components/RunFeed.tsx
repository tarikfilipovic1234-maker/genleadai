"use client";

import { useEffect, useRef } from "react";
import type { AgentEvent } from "@/lib/types";
import type { StreamState } from "@/lib/useTaskStream";

/**
 * The live run, shown as a transcript.
 *
 * A spinner would be dishonest here: the agent takes minutes and makes real
 * decisions, and hiding that behind "Working..." throws away the most
 * interesting thing the product does. Showing each tool call as it happens
 * also makes a slow run legible - you can see it is fetching a website
 * rather than stuck.
 */

const GLYPHS: Record<string, { mark: string; tone: string }> = {
  "run.started": { mark: "▸", tone: "text-verified" },
  "agent.message": { mark: "│", tone: "text-ink" },
  "tool.called": { mark: "→", tone: "text-inferred" },
  "tool.result": { mark: "←", tone: "text-ink-faint" },
  "lead.saved": { mark: "✚", tone: "text-verified" },
  warning: { mark: "!", tone: "text-alert" },
  "run.completed": { mark: "■", tone: "text-verified" },
  "run.failed": { mark: "✕", tone: "text-alert" },
  "stream.end": { mark: "·", tone: "text-ink-faint" },
};

function describe(event: AgentEvent): string {
  const p = event.payload as Record<string, never>;

  switch (event.type) {
    case "run.started":
      return `run started · target ${p.target_count ?? "?"} leads`;
    case "agent.message":
      return String(p.text ?? "");
    case "tool.called": {
      const args = Object.entries((p.input as Record<string, unknown>) ?? {})
        .map(([k, v]) => `${k}=${String(v).slice(0, 42)}`)
        .join(" ");
      return `${p.tool} ${args}`.trim();
    }
    case "tool.result": {
      const summary = Object.entries((p.summary as Record<string, unknown>) ?? {})
        .map(([k, v]) => `${k}=${v}`)
        .join(" ");
      const ms = p.duration_ms ? `${p.duration_ms}ms` : "";
      return [p.tool, ms, summary, p.ok === false ? "FAILED" : ""]
        .filter(Boolean)
        .join(" ");
    }
    case "lead.saved":
      return `saved ${p.name} · ${p.score}/100`;
    case "run.completed": {
      const ledger = (p.ledger as Record<string, unknown>) ?? {};
      return `complete · ${p.leads_saved} leads from ${p.businesses_found} businesses · ${ledger.turns ?? "?"} turns`;
    }
    case "run.failed":
      return `failed · ${p.error}`;
    case "stream.end":
      return String(p.reason ?? "stream closed");
    default:
      return event.type;
  }
}

export function RunFeed({
  events,
  state,
}: {
  events: AgentEvent[];
  state: StreamState;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Follow the tail only when the reader is already there. Yanking the
    // viewport back down while someone is reading an earlier line is the
    // single most irritating thing a live log can do.
    const box = scrollRef.current;
    if (!box) return;
    const atBottom =
      box.scrollHeight - box.scrollTop - box.clientHeight < 120;
    if (atBottom) endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events.length]);

  return (
    <div className="flex h-full flex-col border border-line bg-surface/60">
      <header className="flex items-center justify-between border-b border-line px-3 py-2">
        <span className="rule-label">Agent transcript</span>
        <span className="flex items-center gap-1.5 text-[10px] text-ink-faint">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              state === "open"
                ? "animate-pulse-dot bg-verified"
                : state === "error"
                  ? "bg-alert"
                  : "bg-line-bright"
            }`}
          />
          {state === "open" ? "live" : state === "error" ? "disconnected" : "idle"}
        </span>
      </header>

      <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-2">
        {events.length === 0 ? (
          <p className="py-8 text-center text-[11px] text-ink-faint">
            The agent&apos;s reasoning and every tool call will appear here.
          </p>
        ) : (
          <ol className="space-y-0.5">
            {events.map((event, index) => {
              const glyph = GLYPHS[event.type] ?? {
                mark: "·",
                tone: "text-ink-faint",
              };
              const indent = event.type === "tool.result" ? "pl-6" : "";
              return (
                <li
                  key={`${event.id ?? "x"}-${index}`}
                  className={`animate-slide-in flex gap-2 text-[11px] leading-relaxed ${indent}`}
                >
                  <span className={`shrink-0 ${glyph.tone}`}>{glyph.mark}</span>
                  <span
                    className={
                      event.type === "agent.message"
                        ? "text-ink"
                        : event.type === "run.failed"
                          ? "text-alert"
                          : "text-ink-dim"
                    }
                  >
                    {describe(event)}
                  </span>
                </li>
              );
            })}
          </ol>
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}
