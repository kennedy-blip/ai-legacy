"use client";

import { useEffect, useRef } from "react";

import type { SourceSlice } from "@/lib/types";

/** A slice of a file with a line-number gutter; the focus range is highlighted and scrolled into view. */
export function SourceViewer({
  slice,
  highlight,
  maxHeight = "28rem",
}: {
  slice: SourceSlice;
  /** Overrides the focus range of the slice (e.g. the line a citation points at). */
  highlight?: { start: number; end: number } | null;
  maxHeight?: string;
}) {
  const focusStart = highlight?.start ?? slice.focus?.start_line ?? null;
  const focusEnd = highlight?.end ?? slice.focus?.end_line ?? focusStart;
  const first = useRef<HTMLTableRowElement | null>(null);
  const container = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const row = first.current;
    const box = container.current;
    if (!row || !box) return;
    // Scroll inside the viewer only; never move the page.
    box.scrollTop = Math.max(row.offsetTop - 48, 0);
  }, [slice.path, slice.start_line, focusStart]);

  const lines = slice.text.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();

  return (
    <div className="overflow-hidden rounded-lg border border-line bg-ink">
      <div className="flex items-center justify-between gap-3 border-b border-line px-3 py-1.5 text-xs">
        <span className="truncate font-mono text-dim" title={slice.path}>
          {slice.path}
        </span>
        <span className="shrink-0 text-faint">
          lines {slice.start_line}–{slice.end_line} of {slice.total_lines}
          {slice.head_commit ? ` · ${slice.head_commit.slice(0, 8)}` : ""}
        </span>
      </div>
      <div ref={container} className="overflow-auto" style={{ maxHeight }}>
        <table className="w-full border-collapse font-mono text-[12.5px] leading-[1.55]">
          <tbody>
            {lines.map((text, index) => {
              const number = slice.start_line + index;
              const focused = focusStart !== null && focusEnd !== null && number >= focusStart && number <= focusEnd;
              return (
                <tr
                  key={number}
                  ref={number === focusStart ? first : undefined}
                  className={focused ? "bg-accent/10" : undefined}
                >
                  <td
                    className={`w-12 min-w-12 select-none border-r px-2 text-right align-top tabular-nums ${
                      focused ? "border-accent text-accent" : "border-line text-faint"
                    }`}
                  >
                    {number}
                  </td>
                  <td className="whitespace-pre px-3 text-fg">{text || " "}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
