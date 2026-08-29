"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import { api } from "@/lib/api";
import type { Fact, Lead, Source } from "@/lib/types";
import { CopyButton } from "./CopyButton";
import { FactValue, ProvenanceBar, ProvenanceChip } from "./Provenance";
import { ScoreNumeral } from "./Score";

/**
 * The evidence view for one lead.
 *
 * This is where the provenance system pays off. The list view shows a score;
 * this shows what the score is made of, field by field, each with how it was
 * established and a link to the page it came from. A user who does not trust
 * a lead can check it in two clicks, which is the difference between a tool
 * they act on and one they spot-check forever.
 */

// Grouped in the order a salesperson reads them: who they are, how to reach
// them, what we know about their standing, how they operate.
const GROUPS: { title: string; fields: string[] }[] = [
  {
    title: "Identity",
    fields: ["business_name", "category", "address", "location"],
  },
  {
    title: "Reach",
    fields: ["website", "instagram", "facebook", "phone", "email"],
  },
  { title: "Standing", fields: ["google_rating", "google_review_count"] },
  {
    title: "Operations",
    fields: [
      "has_online_booking",
      "booking_provider",
      "opening_hours",
      "appears_active_online",
      "services_description",
    ],
  },
];

const FIELD_LABELS: Record<string, string> = {
  business_name: "name",
  google_rating: "google rating",
  google_review_count: "review count",
  has_online_booking: "online booking",
  booking_provider: "booking system",
  opening_hours: "opening hours",
  appears_active_online: "active online",
  services_description: "services",
};

export function LeadDetail({
  lead: summary,
  onClose,
  onDeleted,
}: {
  lead: Lead;
  onClose: () => void;
  onDeleted: (id: string) => void;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // The list endpoint omits sources to keep the table payload small, so the
  // full record is fetched here. The summary renders immediately as
  // fallback data, so opening the panel never shows a spinner.
  const { data: lead = summary } = useSWR<Lead>(
    ["lead", summary.id],
    () => api.getLead(summary.id),
    { fallbackData: summary },
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const remove = async () => {
    setDeleting(true);
    try {
      await api.deleteLead(lead.id);
      onDeleted(lead.id);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-ground/70 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden
      />

      <aside
        role="dialog"
        aria-modal="true"
        aria-label={`Evidence for ${lead.name}`}
        className="animate-slide-in fixed inset-y-0 right-0 z-50 flex w-full max-w-[620px] flex-col border-l border-line bg-ground shadow-[-24px_0_60px_rgba(0,0,0,0.55)]"
      >
        <header className="flex items-start justify-between gap-4 border-b border-line px-6 py-4">
          <div className="min-w-0">
            <h2 className="truncate font-display text-3xl leading-tight text-ink">
              {lead.name}
            </h2>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-ink-faint">
              {lead.category && <span>{lead.category}</span>}
              <ProvenanceBar summary={lead.provenance_summary} />
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <ScoreNumeral score={lead.score} size="lg" />
            <button
              onClick={onClose}
              aria-label="Close"
              className="border border-line px-2 py-1 text-ink-faint transition-colors hover:border-line-bright hover:text-ink"
            >
              ✕
            </button>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-5">
          <ScoreBreakdown lead={lead} />
          <Reasoning lead={lead} />
          <Outreach lead={lead} />
          <Evidence facts={lead.facts} />
          <Sources sources={lead.sources ?? []} />
        </div>

        <footer className="flex items-center justify-between gap-3 border-t border-line px-6 py-3">
          <span className="text-[10px] text-ink-faint">
            found {new Date(lead.created_at).toLocaleString()}
          </span>
          {confirmingDelete ? (
            <div className="flex items-center gap-2">
              <span className="text-[10px] text-ink-dim">Delete this lead?</span>
              <button
                onClick={remove}
                disabled={deleting}
                className="border border-alert/50 bg-alert/10 px-2.5 py-1 text-[10px] tracking-[0.12em] text-alert uppercase disabled:opacity-50"
              >
                {deleting ? "Deleting…" : "Confirm"}
              </button>
              <button
                onClick={() => setConfirmingDelete(false)}
                className="border border-line px-2.5 py-1 text-[10px] tracking-[0.12em] text-ink-faint uppercase"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={() => setConfirmingDelete(true)}
              className="border border-line px-2.5 py-1 text-[10px] tracking-[0.12em] text-ink-faint uppercase transition-colors hover:border-alert/50 hover:text-alert"
            >
              Delete
            </button>
          )}
        </footer>
      </aside>
    </>
  );
}

function Section({
  title,
  children,
  action,
}: {
  title: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <section className="mb-7">
      <div className="mb-2.5 flex items-center justify-between gap-3">
        <h3 className="rule-label">{title}</h3>
        {action}
      </div>
      {children}
    </section>
  );
}

function ScoreBreakdown({ lead }: { lead: Lead }) {
  if (lead.score_breakdown.length === 0) return null;
  return (
    <Section title={`Score — ${lead.score} of 100`}>
      <ul className="space-y-1">
        {lead.score_breakdown.map((c) => (
          <li
            key={c.rule}
            className="flex items-baseline gap-3 border-l border-verified/40 pl-3 text-[11px]"
          >
            <span className="w-8 shrink-0 tabular-nums text-verified">
              +{c.points}
            </span>
            <span className="text-ink-dim">{c.reason}</span>
          </li>
        ))}
      </ul>
    </Section>
  );
}

function Reasoning({ lead }: { lead: Lead }) {
  if (!lead.qualification_reason && !lead.sales_angle) return null;
  return (
    <Section title="Assessment">
      {lead.qualification_reason && (
        <p className="text-[11.5px] leading-relaxed text-ink-dim">
          {lead.qualification_reason}
        </p>
      )}
      {lead.sales_angle && (
        <p className="mt-3 border-l border-line-bright pl-3 text-[11.5px] leading-relaxed text-ink">
          {lead.sales_angle}
        </p>
      )}
    </Section>
  );
}

function Outreach({ lead }: { lead: Lead }) {
  if (!lead.outreach_message) return null;
  return (
    <Section
      title="Outreach"
      action={<CopyButton text={lead.outreach_message} label="Copy message" />}
    >
      <p className="border border-line bg-surface/60 p-4 text-[11.5px] leading-relaxed whitespace-pre-wrap text-ink">
        {lead.outreach_message}
      </p>
    </Section>
  );
}

function Evidence({ facts }: { facts: Record<string, Fact> }) {
  return (
    <Section title="Evidence">
      <div className="space-y-5">
        {GROUPS.map((group) => {
          const rows = group.fields.filter((f) => f in facts);
          if (rows.length === 0) return null;
          return (
            <div key={group.title}>
              <p className="mb-1.5 text-[10px] text-ink-faint">{group.title}</p>
              <dl className="divide-y divide-line border-y border-line">
                {rows.map((field) => (
                  <FactRow key={field} field={field} fact={facts[field]} />
                ))}
              </dl>
            </div>
          );
        })}
      </div>
    </Section>
  );
}

function FactRow({ field, fact }: { field: string; fact: Fact }) {
  return (
    <div className="flex items-start gap-3 py-2">
      <dt className="w-32 shrink-0 pt-px text-[10.5px] text-ink-faint">
        {FIELD_LABELS[field] ?? field.replace(/_/g, " ")}
      </dt>
      <dd className="flex min-w-0 flex-1 items-start gap-2">
        <ProvenanceChip provenance={fact.provenance} />
        <div className="min-w-0 flex-1">
          <div className="text-[11.5px] break-words">
            <FactValue fact={fact} />
          </div>
          {/* The evidence line is why a claim is checkable rather than just
              labelled. For an unverified field it says what was tried. */}
          {fact.evidence && (
            <p className="mt-0.5 text-[10px] leading-snug text-ink-faint">
              {fact.evidence}
            </p>
          )}
          {fact.source_url && (
            <a
              href={fact.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-0.5 inline-block text-[10px] text-ink-faint underline decoration-line-bright underline-offset-2 hover:text-verified"
            >
              {sourceLabel(fact.source_url)}
            </a>
          )}
        </div>
      </dd>
    </div>
  );
}

function Sources({ sources }: { sources: Source[] }) {
  if (sources.length === 0) return null;
  return (
    <Section title={`Sources — ${sources.length}`}>
      <ul className="space-y-2">
        {sources.map((source) => (
          <li key={source.url} className="border border-line bg-surface/40 p-3">
            <div className="flex items-center gap-2">
              <span className="rounded-sm border border-line-bright px-1.5 text-[9px] tracking-[0.1em] text-ink-faint uppercase">
                {source.kind}
              </span>
              <a
                href={source.url}
                target="_blank"
                rel="noopener noreferrer"
                className="truncate text-[11px] text-ink-dim underline decoration-line-bright underline-offset-2 hover:text-verified"
              >
                {source.title || sourceLabel(source.url)}
              </a>
            </div>
            {source.excerpt && (
              <p className="mt-1.5 line-clamp-3 text-[10.5px] leading-relaxed text-ink-faint">
                {source.excerpt}
              </p>
            )}
          </li>
        ))}
      </ul>
    </Section>
  );
}

function sourceLabel(url: string): string {
  try {
    const parsed = new URL(url);
    return parsed.host.replace(/^www\./, "") + parsed.pathname.replace(/\/$/, "");
  } catch {
    return url;
  }
}
