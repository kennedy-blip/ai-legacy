"use client";

import type { ReactNode } from "react";

import { displayName, labelName, toneOf, TONE_TEXT } from "@/lib/format";
import type { NodeBrief } from "@/lib/types";
import { Badge, Dot } from "@/ui/common/primitives";

import { useWorkspace, type TabId } from "./context";

/** One row describing a node; clicking it selects the node. */
export function NodeRow({
  node,
  tab = "explore",
  active = false,
  trailing,
  subtitle,
}: {
  node: NodeBrief;
  tab?: TabId;
  active?: boolean;
  trailing?: ReactNode;
  subtitle?: ReactNode;
}) {
  const { open } = useWorkspace();
  const tone = toneOf(node.label, node.kind);
  return (
    <button
      type="button"
      onClick={() => open(node.id, tab)}
      className={`flex w-full min-w-0 items-start gap-2 px-3 py-1.5 text-left text-sm transition-colors hover:bg-hover ${
        active ? "bg-raised" : ""
      }`}
    >
      <span className="mt-1.5">
        <Dot tone={tone} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="truncate font-mono text-[13px]">{displayName(node)}</span>
          <NodeFlags node={node} />
        </span>
        <span className="block truncate text-xs text-faint">
          {subtitle ?? (
            <>
              {labelName(node.label, node.kind)}
              {node.location ? ` · ${node.location}` : ""}
            </>
          )}
        </span>
      </span>
      {trailing ? <span className="shrink-0 text-xs text-faint">{trailing}</span> : null}
    </button>
  );
}

export function NodeFlags({ node }: { node: NodeBrief }) {
  return (
    <>
      {node.is_entry_point ? (
        <Badge tone="accent" title="Execution enters the code base here">
          {node.http_method ? `${node.http_method} ${node.route ?? ""}`.trim() : (node.entry_kind ?? "entry")}
        </Badge>
      ) : null}
      {node.dead_candidate ? (
        <Badge tone="danger" title="No caller found by static analysis. Reflection and external callers are not visible.">
          no callers found
        </Badge>
      ) : null}
      {node.is_test ? <Badge>test</Badge> : null}
    </>
  );
}

/** Inline, clickable node name for running text. */
export function NodeLink({ node, tab = "explore" }: { node: NodeBrief; tab?: TabId }) {
  const { open } = useWorkspace();
  return (
    <button
      type="button"
      onClick={() => open(node.id, tab)}
      className={`font-mono text-[13px] underline decoration-line-strong underline-offset-2 hover:decoration-current ${
        TONE_TEXT[toneOf(node.label, node.kind)]
      }`}
    >
      {displayName(node)}
    </button>
  );
}
