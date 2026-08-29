"use client";

import type { Lead } from "@/lib/types";
import { FactValue, ProvenanceBar } from "./Provenance";
import { ScoreMeter, ScoreNumeral } from "./Score";

/**
 * The result set.
 *
 * Rows are cards rather than table cells because the interesting content per
 * lead - a score breakdown, a provenance mix, a booking verdict with its
 * caveat - does not fit a grid without becoming unreadable. Density is kept
 * by tightening the type, not by removing information.
 */
export function LeadTable({
  leads,
  selectedId,
  onSelect,
  loading,
}: {
  leads: Lead[];
  selectedId: string | null;
  onSelect: (lead: Lead) => void;
  loading?: boolean;
}) {
  if (loading && leads.length === 0) {
    return (
      <div className="space-y-2">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="h-[86px] animate-pulse border border-line bg-surface/40"
            style={{ animationDelay: `${i * 120}ms` }}
          />
        ))}
      </div>
    );
  }

  if (leads.length === 0) {
    return (
      <div className="border border-dashed border-line px-6 py-16 text-center">
        <p className="font-display text-xl text-ink-dim">No leads yet</p>
        <p className="mt-2 text-[11px] text-ink-faint">
          Start a run above, or relax the score filter.
        </p>
      </div>
    );
  }

  return (
    <ol className="space-y-1.5">
      {leads.map((lead, index) => (
        <li
          key={lead.id}
          className="animate-slide-in"
          style={{ animationDelay: `${Math.min(index * 35, 350)}ms` }}
        >
          <LeadRow
            lead={lead}
            selected={lead.id === selectedId}
            onSelect={() => onSelect(lead)}
          />
        </li>
      ))}
    </ol>
  );
}

function LeadRow({
  lead,
  selected,
  onSelect,
}: {
  lead: Lead;
  selected: boolean;
  onSelect: () => void;
}) {
  const booking = lead.facts.has_online_booking;
  const instagram = lead.facts.instagram;

  return (
    <button
      onClick={onSelect}
      aria-current={selected}
      className={`group grain relative w-full border px-4 py-3 text-left transition-colors ${
        selected
          ? "border-verified/50 bg-raised"
          : "border-line bg-surface/50 hover:border-line-bright hover:bg-raised/70"
      }`}
    >
      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <h3 className="truncate font-display text-lg leading-tight text-ink">
              {lead.name}
            </h3>
            {lead.category && (
              <span className="shrink-0 text-[10px] text-ink-faint">
                {lead.category}
              </span>
            )}
          </div>

          {/* The two signals the flagship query is actually about, stated
              plainly on the row so the list is scannable without opening
              anything. */}
          <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
            <span className="text-ink-faint">
              booking:{" "}
              {booking ? <FactValue fact={booking} /> : <span>unknown</span>}
            </span>
            {instagram && instagram.provenance !== "unverified" && (
              <span className="text-ink-faint">
                instagram: <FactValue fact={instagram} />
              </span>
            )}
          </div>

          {lead.qualification_reason && (
            <p className="mt-2 line-clamp-2 max-w-2xl text-[11px] leading-relaxed text-ink-dim">
              {lead.qualification_reason}
            </p>
          )}
        </div>

        <div className="flex shrink-0 flex-col items-end gap-2 pt-0.5">
          <ScoreNumeral score={lead.score} />
          <div className="w-28">
            <ScoreMeter breakdown={lead.score_breakdown} />
          </div>
          <ProvenanceBar summary={lead.provenance_summary} compact />
        </div>
      </div>
    </button>
  );
}
