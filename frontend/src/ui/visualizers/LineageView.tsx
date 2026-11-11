"use client";

import { useMemo } from "react";

import type { Lineage, LineageLayer } from "@/lib/types";

import { LayeredGraph, type GraphColumn } from "./LayeredGraph";

const LAYERS: { id: LineageLayer; title: string }[] = [
  { id: "entry", title: "Entry points" },
  { id: "logic", title: "Code" },
  { id: "query", title: "SQL statements" },
  { id: "table", title: "Tables" },
  { id: "side_effect", title: "Hidden side effects" },
];

/** From the entry points down to the rows: which code reaches which table, and what the database does next. */
export function LineageView({
  lineage,
  selectedId,
  onSelect,
}: {
  lineage: Lineage;
  selectedId?: string | null;
  onSelect: (nodeId: string) => void;
}) {
  const columns = useMemo<GraphColumn[]>(
    () =>
      LAYERS.map((layer) => ({
        id: layer.id,
        title: layer.title,
        nodes: lineage.nodes.filter((node) => (lineage.layers[node.id] ?? "logic") === layer.id),
      })),
    [lineage],
  );

  return (
    <LayeredGraph
      columns={columns}
      edges={lineage.edges}
      rootId={lineage.root.id}
      selectedId={selectedId}
      onSelect={onSelect}
      ariaLabel={`Data lineage of ${lineage.root.qualified_name ?? lineage.root.name}: ${lineage.nodes.length} nodes`}
    />
  );
}
