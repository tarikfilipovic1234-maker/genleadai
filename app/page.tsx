"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import { LeadTable } from "@/components/LeadTable";
import { ProvenanceLegend } from "@/components/Provenance";
import { RunFeed } from "@/components/RunFeed";
import { TaskForm } from "@/components/TaskForm";
import { api, ApiError } from "@/lib/api";
import type { BackendConfig, Lead } from "@/lib/types";
import { useTaskStream } from "@/lib/useTaskStream";

type Order = "score" | "name" | "created";

export default function Dashboard() {
  const [taskId, setTaskId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);

  const [minScore, setMinScore] = useState(0);
  const [search, setSearch] = useState("");
  const [order, setOrder] = useState<Order>("score");

  const stream = useTaskStream(taskId);

  // Three states, not two. `undefined` is "still asking", `null` is "asked
  // and could not reach it". Collapsing them makes the header flash
  // "backend unreachable" on every load before the first response lands.
  const { data: config } = useSWR<BackendConfig | null>("config", () =>
    api.config().catch(() => null),
  );

  // Keyed on the task and sort order, so a response for a previous run can
  // never overwrite the current one - the failure mode of hand-rolled
  // fetch-into-state when two runs are started in quick succession.
  const {
    data: leads = [],
    error: leadsError,
    isLoading,
    mutate,
  } = useSWR<Lead[]>(["leads", taskId, order], () =>
    api.listLeads({ taskId: taskId ?? undefined, order }),
  );

  // Revalidate exactly when the stream says there is something new, rather
  // than polling: faster to appear, and idle when nothing is happening.
  useEffect(() => {
    void mutate();
  }, [stream.leadCount, stream.finished, mutate]);

  const startRun = useCallback(
    async (prompt: string, target: number, profile?: string) => {
      setStartError(null);
      setSelectedId(null);
      try {
        const created = await api.createTask(prompt, target, profile);
        setTaskId(created.id);
      } catch (e) {
        setStartError(e instanceof ApiError ? e.message : String(e));
      }
    },
    [],
  );

  const error =
    startError ??
    (leadsError instanceof ApiError ? leadsError.message : null);

  // Filtering happens client-side because the result set is at most a few
  // hundred rows and a round trip per keystroke would feel worse than it
  // costs. Sorting stays server-side, where the index is.
  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return leads.filter(
      (lead) =>
        lead.score >= minScore &&
        (!needle || lead.name.toLowerCase().includes(needle)),
    );
  }, [leads, minScore, search]);

  const running = stream.state === "open";
  const awaitingFirstLead = isLoading || (running && leads.length === 0);

  return (
    <div className="mx-auto max-w-[1500px] px-6 py-8 lg:px-10">
      <Header config={config} />

      <div className="mt-8">
        <TaskForm
          onSubmit={startRun}
          running={running}
          config={config}
          error={error}
        />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,1fr)_400px]">
        <main>
          <Toolbar
            count={visible.length}
            total={leads.length}
            minScore={minScore}
            onMinScore={setMinScore}
            search={search}
            onSearch={setSearch}
            order={order}
            onOrder={setOrder}
            taskId={taskId}
          />
          <div className="mt-3">
            <LeadTable
              leads={visible}
              selectedId={selectedId}
              onSelect={(lead) => setSelectedId(lead.id)}
              loading={awaitingFirstLead}
            />
          </div>
        </main>

        {/* Sticky: on a long result set the transcript is the thing you keep
            glancing back at while a run is in progress. */}
        <aside className="lg:sticky lg:top-8 lg:h-[calc(100vh-6rem)]">
          <RunFeed events={stream.events} state={stream.state} />
        </aside>
      </div>

      <footer className="mt-10 border-t border-line pt-5">
        <ProvenanceLegend />
      </footer>
    </div>
  );
}

function Header({ config }: { config: BackendConfig | null | undefined }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4 border-b border-line pb-5">
      <div>
        <h1 className="font-display text-4xl leading-none tracking-tight text-ink">
          Lead Research Agent
        </h1>
        {/* States the actual thesis rather than a feature list. */}
        <p className="mt-2 max-w-xl text-[11px] leading-relaxed text-ink-dim">
          Researches real businesses against real sources, and records what it{" "}
          <span className="text-verified">verified</span>, what it{" "}
          <span className="text-inferred">inferred</span>, and what it could not
          confirm.
        </p>
      </div>

      <div className="flex items-center gap-4 text-[10px] text-ink-faint">
        {config === undefined ? (
          <span className="border border-line px-2 py-1 text-ink-faint">
            connecting…
          </span>
        ) : config ? (
          <span className="border border-line px-2 py-1">
            runtime <span className="text-ink-dim">{config.runtime}</span>
          </span>
        ) : (
          <span className="border border-alert/40 px-2 py-1 text-alert">
            backend unreachable
          </span>
        )}
      </div>
    </header>
  );
}

function Toolbar({
  count,
  total,
  minScore,
  onMinScore,
  search,
  onSearch,
  order,
  onOrder,
  taskId,
}: {
  count: number;
  total: number;
  minScore: number;
  onMinScore: (v: number) => void;
  search: string;
  onSearch: (v: string) => void;
  order: Order;
  onOrder: (v: Order) => void;
  taskId: string | null;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-3 border border-line bg-surface/50 px-4 py-2.5">
      <span className="font-display text-lg leading-none text-ink">
        {count}
        {count !== total && (
          <span className="text-ink-faint"> of {total}</span>
        )}
        <span className="ml-1.5 text-[10px] tracking-[0.14em] text-ink-faint uppercase">
          leads
        </span>
      </span>

      <div className="h-4 w-px bg-line" />

      <label className="flex items-center gap-2 text-[10px] text-ink-faint">
        <span className="tracking-[0.12em] uppercase">min score</span>
        <input
          type="range"
          min={0}
          max={100}
          step={10}
          value={minScore}
          onChange={(e) => onMinScore(Number(e.target.value))}
          className="w-24 accent-[var(--color-verified)]"
        />
        <span className="w-6 tabular-nums text-ink-dim">{minScore}</span>
      </label>

      <input
        value={search}
        onChange={(e) => onSearch(e.target.value)}
        placeholder="filter by name"
        className="min-w-[140px] flex-1 border-b border-line bg-transparent pb-1 text-[11px] outline-none placeholder:text-ink-faint focus:border-verified/60"
      />

      <div className="flex items-center gap-1">
        {(["score", "name", "created"] as Order[]).map((key) => (
          <button
            key={key}
            onClick={() => onOrder(key)}
            className={`px-2 py-1 text-[10px] tracking-[0.1em] uppercase transition-colors ${
              order === key
                ? "text-verified"
                : "text-ink-faint hover:text-ink-dim"
            }`}
          >
            {key}
          </button>
        ))}
      </div>

      <a
        href={api.csvUrl(taskId ?? undefined)}
        className="border border-line px-2.5 py-1 text-[10px] tracking-[0.12em] text-ink-faint uppercase transition-colors hover:border-line-bright hover:text-ink-dim"
      >
        CSV
      </a>
    </div>
  );
}
