"use client";

import { useState } from "react";
import type { BackendConfig } from "@/lib/types";

const EXAMPLES = [
  "Find 10 beauty salons in Sarajevo that don't have online booking",
  "Find 8 restaurants in Sarajevo with an outdated website",
  "Find 10 dentists in Sarajevo with no online booking and an active Instagram",
];

const PROFILES = [
  { value: "", label: "Rank everything" },
  { value: "no_online_booking", label: "Require: no online booking" },
  { value: "no_booking_with_social", label: "Require: no booking + Instagram" },
];

export function TaskForm({
  onSubmit,
  running,
  config,
  error,
}: {
  onSubmit: (prompt: string, target: number, profile?: string) => void;
  running: boolean;
  config: BackendConfig | null | undefined;
  error: string | null;
}) {
  const [prompt, setPrompt] = useState(EXAMPLES[0]);
  const [target, setTarget] = useState(5);
  const [profile, setProfile] = useState("");

  const liveDisabled = config != null && !config.live_runs_enabled;
  const canSubmit = prompt.trim().length >= 8 && !running;

  return (
    <section className="border border-line bg-surface/50">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit) onSubmit(prompt.trim(), target, profile || undefined);
        }}
        className="p-5"
      >
        <label htmlFor="prompt" className="rule-label">
          Describe the leads you want
        </label>

        <textarea
          id="prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={2}
          spellCheck={false}
          className="mt-2 w-full resize-none border-0 border-b border-line bg-transparent pb-3 font-display text-2xl leading-snug text-ink outline-none transition-colors placeholder:text-ink-faint focus:border-verified/60"
          placeholder="Find 10 beauty salons in Sarajevo without online booking"
        />

        <div className="mt-4 flex flex-wrap items-end justify-between gap-4">
          <div className="flex flex-wrap items-end gap-5">
            <Field label="Target leads">
              <input
                type="number"
                min={1}
                max={50}
                value={target}
                onChange={(e) => setTarget(Number(e.target.value))}
                className="w-16 border-b border-line bg-transparent pb-1 text-center tabular-nums outline-none focus:border-verified/60"
              />
            </Field>

            <Field label="Qualification">
              <select
                value={profile}
                onChange={(e) => setProfile(e.target.value)}
                className="cursor-pointer border-b border-line bg-transparent pb-1 pr-1 text-ink outline-none focus:border-verified/60"
              >
                {PROFILES.map((p) => (
                  <option key={p.value} value={p.value} className="bg-raised">
                    {p.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          <button
            type="submit"
            disabled={!canSubmit}
            className="group relative border border-verified/50 bg-verified/10 px-6 py-2.5 text-[11px] tracking-[0.16em] text-verified uppercase transition-all hover:bg-verified/20 disabled:cursor-not-allowed disabled:border-line disabled:bg-transparent disabled:text-ink-faint"
          >
            {running ? "Researching…" : "Find leads"}
          </button>
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => setPrompt(example)}
              className="border border-line px-2.5 py-1 text-[10px] text-ink-faint transition-colors hover:border-line-bright hover:text-ink-dim"
            >
              {example.replace(/^Find \d+ /, "")}
            </button>
          ))}
        </div>
      </form>

      {/* The deployed instance serves recorded runs. Saying so plainly is
          better than a button that silently does something else. */}
      {liveDisabled && (
        <p className="border-t border-line bg-raised/50 px-5 py-2.5 text-[10px] leading-relaxed text-ink-faint">
          This deployment replays a recorded run against real Sarajevo
          businesses — the events, timings, leads and sources are all from an
          actual run. It holds no API credentials and the Claude libraries are
          not installed, so it cannot call a model. Clone the repository to run
          the agent live.
          {/* The backend sleeps on the free tier, so the first request after
              an idle period takes about a minute. Saying so turns a broken-
              looking wait into an expected one. */}
          <span className="mt-1 block">
            The backend sleeps when idle — the first run after a quiet spell
            can take up to a minute to wake.
          </span>
        </p>
      )}

      {error && (
        <p className="border-t border-alert/30 bg-alert/8 px-5 py-2.5 text-[11px] text-alert">
          {error}
        </p>
      )}
    </section>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <span className="rule-label block">{label}</span>
      <div className="mt-1.5 text-[12px]">{children}</div>
    </div>
  );
}
