"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Copy to clipboard with confirmation.
 *
 * The fallback matters more than it looks: navigator.clipboard is only
 * available in secure contexts, so it is undefined on any deployment served
 * over plain http and on some in-app browsers. Without the fallback the copy
 * button silently does nothing in exactly the environments where a user is
 * most likely to be trying it from their phone.
 */
export function CopyButton({
  text,
  label = "Copy",
  className = "",
}: {
  text: string;
  label?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  const copy = useCallback(async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const area = document.createElement("textarea");
        area.value = text;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        document.body.removeChild(area);
      }
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1600);
    } catch {
      // Some browsers reject the permission outright. Saying so beats a
      // button that appears to have worked.
      window.prompt("Copy this message:", text);
    }
  }, [text]);

  return (
    <button
      onClick={copy}
      className={`border px-2.5 py-1 text-[10px] tracking-[0.12em] uppercase transition-colors ${
        copied
          ? "border-verified/50 text-verified"
          : "border-line text-ink-faint hover:border-line-bright hover:text-ink-dim"
      } ${className}`}
    >
      {copied ? "Copied" : label}
    </button>
  );
}
