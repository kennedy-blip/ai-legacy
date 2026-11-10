"use client";

import { displayName } from "@/lib/format";
import type { NodeBrief } from "@/lib/types";

/** Choose where a diagram starts: one chip per candidate, the current one highlighted. */
export function StartPicker({
  label,
  options,
  selected,
  onPick,
}: {
  label: string;
  options: NodeBrief[];
  selected: string | null;
  onPick: (nodeId: string) => void;
}) {
  if (options.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="eyebrow mr-1">{label}</span>
      {options.slice(0, 14).map((node) => {
        const text = node.http_method ? `${node.http_method} ${node.route ?? ""}`.trim() : displayName(node);
        const active = node.id === selected;
        return (
          <button
            key={node.id}
            type="button"
            title={`${displayName(node)}${node.location ? ` · ${node.location}` : ""}`}
            onClick={() => onPick(node.id)}
            className={`max-w-64 truncate rounded-full border px-2.5 py-1 font-mono text-xs transition-colors ${
              active
                ? "border-accent bg-accent/10 text-accent"
                : "border-line-strong text-dim hover:border-faint hover:text-fg"
            }`}
          >
            {text}
          </button>
        );
      })}
      {options.length > 14 ? <span className="text-xs text-faint">+{options.length - 14} more in Overview</span> : null}
    </div>
  );
}
