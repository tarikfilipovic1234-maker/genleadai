import type { Fact, Provenance } from "@/lib/types";

/**
 * The provenance visual language, defined once.
 *
 * Amber is reserved for verified and appears nowhere else in the interface.
 * The moment it decorates a button, it stops meaning "read from a source"
 * and the whole system stops carrying information.
 */
const STYLES: Record<
  Provenance,
  { chip: string; text: string; label: string; title: string }
> = {
  verified: {
    chip: "border-verified/45 bg-verified/12 text-verified",
    text: "text-ink",
    label: "VER",
    title: "Read directly from a named source",
  },
  inferred: {
    // Dashed border: visibly provisional at a glance, without needing the
    // legend.
    chip: "border-dashed border-inferred/50 bg-inferred/8 text-inferred",
    text: "text-ink-dim",
    label: "INF",
    title: "The agent's judgement from evidence it saw",
  },
  unverified: {
    chip: "border-line-bright bg-transparent text-ink-faint hatched",
    text: "text-ink-faint",
    label: "—",
    title: "Looked for and could not confirm",
  },
};

export function ProvenanceChip({ provenance }: { provenance: Provenance }) {
  const style = STYLES[provenance];
  return (
    <span
      title={style.title}
      className={`inline-flex shrink-0 items-center rounded-sm border px-1.5 text-[9px] leading-[16px] tracking-[0.12em] ${style.chip}`}
    >
      {style.label}
    </span>
  );
}

/**
 * One fact, rendered so the reader can never mistake a gap for a value.
 *
 * "Not verified" is written out in full rather than left as an empty cell.
 * An empty cell reads as "this business has no phone number"; the point of
 * the system is that those are different statements.
 */
export function FactValue({ fact }: { fact: Fact }) {
  if (fact.provenance === "unverified") {
    return (
      <span
        className="text-ink-faint italic"
        title={fact.evidence ?? "Not established"}
      >
        Not verified
      </span>
    );
  }

  const display =
    typeof fact.value === "boolean" ? (fact.value ? "yes" : "no") : String(fact.value);

  const isLink =
    typeof fact.value === "string" && /^https?:\/\//.test(fact.value);

  return (
    <span className={STYLES[fact.provenance].text}>
      {isLink ? (
        <a
          href={fact.value as string}
          target="_blank"
          rel="noopener noreferrer"
          className="underline decoration-line-bright underline-offset-2 hover:decoration-verified"
        >
          {display.replace(/^https?:\/\//, "").replace(/\/$/, "")}
        </a>
      ) : (
        display
      )}
    </span>
  );
}

/**
 * A lead's evidence at a glance: how many facts are sourced, judged, unknown.
 *
 * Shown as proportional bars rather than three numbers because the useful
 * question is "how much of this is actually established", which is a shape,
 * not an arithmetic.
 */
export function ProvenanceBar({
  summary,
  compact = false,
}: {
  summary: Record<Provenance, number>;
  compact?: boolean;
}) {
  const total =
    (summary.verified ?? 0) + (summary.inferred ?? 0) + (summary.unverified ?? 0);
  if (!total) return null;

  const segments: { key: Provenance; className: string }[] = [
    { key: "verified", className: "bg-verified" },
    { key: "inferred", className: "bg-inferred/70" },
    { key: "unverified", className: "bg-line-bright" },
  ];

  return (
    <div className="flex items-center gap-2">
      <div
        className={`flex ${compact ? "h-1 w-16" : "h-1.5 w-28"} overflow-hidden rounded-full bg-line`}
        title={`${summary.verified} verified · ${summary.inferred} inferred · ${summary.unverified} unverified`}
      >
        {segments.map(({ key, className }) =>
          summary[key] ? (
            <div
              key={key}
              className={className}
              style={{ width: `${(summary[key] / total) * 100}%` }}
            />
          ) : null,
        )}
      </div>
      {!compact && (
        <span className="text-[10px] text-ink-faint tabular-nums">
          {summary.verified}/{total}
        </span>
      )}
    </div>
  );
}

export function ProvenanceLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
      {(Object.keys(STYLES) as Provenance[]).map((key) => (
        <div key={key} className="flex items-center gap-2">
          <ProvenanceChip provenance={key} />
          <span className="text-[10px] text-ink-faint">{STYLES[key].title}</span>
        </div>
      ))}
    </div>
  );
}
