import type { ScoreContribution } from "@/lib/types";

/**
 * A score, shown as its parts.
 *
 * A single filled bar would say "85" and stop. Because scoring here is
 * arithmetic over named rules, the breakdown is available - and showing the
 * segments turns an assertion into an explanation. Hovering names the rule
 * that contributed each one.
 */
export function ScoreMeter({
  breakdown,
  max = 100,
}: {
  breakdown: ScoreContribution[];
  max?: number;
}) {
  return (
    <div className="w-full">
      <div className="flex h-1.5 w-full gap-px overflow-hidden rounded-full bg-line">
        {breakdown.map((c) => (
          <div
            key={c.rule}
            className="bg-verified/80 transition-colors hover:bg-verified"
            style={{ width: `${(c.points / max) * 100}%` }}
            title={`${c.rule} +${c.points} — ${c.reason}`}
          />
        ))}
      </div>
    </div>
  );
}

/**
 * The numeral itself.
 *
 * Tiers are deliberately blunt: above 70 is worth a call today, 40-70 is
 * worth a look, below 40 usually means the website could not be read and the
 * lead is mostly unknowns. Colour follows that, not a gradient.
 */
export function ScoreNumeral({
  score,
  size = "md",
}: {
  score: number;
  size?: "sm" | "md" | "lg";
}) {
  const tone =
    score >= 70 ? "text-verified" : score >= 40 ? "text-inferred" : "text-ink-faint";

  const sizes = {
    sm: "text-lg",
    md: "text-2xl",
    lg: "text-5xl",
  } as const;

  return (
    <span className={`font-display tabular-nums leading-none ${sizes[size]} ${tone}`}>
      {score}
      <span className="ml-0.5 text-[0.45em] text-ink-faint">/100</span>
    </span>
  );
}
