"use client";

import { useMemo } from "react";

import type { CallChain } from "@/lib/types";

import { LayeredGraph, type GraphColumn } from "./LayeredGraph";

function columnTitle(level: number): string {
  if (level === 0) return "selected";
  const hops = Math.abs(level);
  const side = level < 0 ? "callers" : "callees";
  return hops === 1 ? `direct ${side}` : `${side}, ${hops} hops`;
}

/** Who reaches the selected function (left) and what it reaches (right). */
export function CallGraph({
  chain,
  selectedId,
  onSelect,
}: {
  chain: CallChain;
  selectedId?: string | null;
  onSelect: (nodeId: string) => void;
}) {
  const columns = useMemo<GraphColumn[]>(() => {
    const byLevel = new Map<number, GraphColumn>();
    for (const node of chain.nodes) {
      const level = chain.levels[node.id] ?? 0;
      let column = byLevel.get(level);
      if (!column) {
        column = { id: `level:${level}`, title: columnTitle(level), nodes: [] };
        byLevel.set(level, column);
      }
      column.nodes.push(node);
    }
    return [...byLevel.entries()].sort((a, b) => a[0] - b[0]).map(([, column]) => column);
  }, [chain]);

  return (
    <LayeredGraph
      columns={columns}
      edges={chain.edges}
      rootId={chain.root.id}
      selectedId={selectedId}
      onSelect={onSelect}
      ariaLabel={`Call graph around ${chain.root.qualified_name ?? chain.root.name}: ${chain.nodes.length} nodes`}
    />
  );
}
