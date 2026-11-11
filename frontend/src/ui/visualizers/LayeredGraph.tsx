"use client";

import { useMemo, useState } from "react";

import { displayName, labelName, toneOf, TONE_HEX, truncate } from "@/lib/format";
import type { GraphEdge, NodeBrief, RelType } from "@/lib/types";
import { ScrollArea } from "@/ui/common/ScrollArea";

export interface GraphColumn {
  id: string;
  title: string;
  nodes: NodeBrief[];
}

interface Props {
  columns: GraphColumn[];
  edges: GraphEdge[];
  rootId?: string | null;
  selectedId?: string | null;
  onSelect: (nodeId: string) => void;
  ariaLabel: string;
}

const BOX_W = 200;
const BOX_H = 40;
const GAP_X = 64;
const GAP_Y = 10;
const PAD = 16;
const HEADER = 26;
/** A graph wider than its panel is drawn smaller, down to this share of full size; beyond that it scrolls. */
const MIN_SCALE = 0.72;

interface Placed {
  node: NodeBrief;
  column: number;
  x: number;
  y: number;
}

interface DrawnEdge {
  key: string;
  edge: GraphEdge;
  path: string;
  /** Times the relationship was seen at runtime, when telemetry confirms a static edge. */
  observed: number | null;
  simulated: boolean;
  midX: number;
  midY: number;
}

const STATIC_OF: Partial<Record<RelType, RelType>> = {
  OBSERVED_CALL: "CALLS",
  OBSERVED_READ: "READS_FROM_DB",
  OBSERVED_WRITE: "MUTATES_TABLE",
};

const STROKE = {
  call: "#6f7b91",
  contains: "#3d4658",
  read: TONE_HEX.qry,
  write: TONE_HEX.tbl,
  trigger: TONE_HEX.danger,
  runtime: TONE_HEX.runtime,
} as const;

type StrokeKind = keyof typeof STROKE;

function strokeKind(type: RelType): StrokeKind {
  switch (type) {
    case "READS_FROM_DB":
      return "read";
    case "MUTATES_TABLE":
      return "write";
    case "FIRES_TRIGGER":
      return "trigger";
    case "CONTAINS":
      return "contains";
    case "OBSERVED_CALL":
    case "OBSERVED_READ":
    case "OBSERVED_WRITE":
      return "runtime";
    default:
      return "call";
  }
}

function describe(edge: GraphEdge, observed: number | null, simulated: boolean): string {
  const parts: string[] = [edge.type.replaceAll("_", " ").toLowerCase()];
  if (edge.operations?.length) parts.push(edge.operations.join(", "));
  if (edge.events?.length) parts.push(`on ${edge.events.join(", ")}`);
  if (Array.isArray(edge.lines) && edge.lines.length) parts.push(`line ${edge.lines.join(", ")}`);
  if (edge.confidence < 1 && edge.type === "CALLS") {
    parts.push(`confidence ${Math.round(edge.confidence * 100)}%${edge.resolution ? ` (${edge.resolution})` : ""}`);
  }
  const count = observed ?? edge.count ?? null;
  if (count !== null && (observed !== null || edge.type.startsWith("OBSERVED"))) {
    parts.push(`${simulated ? "simulated traffic" : "observed at runtime"}: ${count} times`);
  }
  return parts.join(" · ");
}

/** Order nodes inside each column so connected nodes sit near each other. */
function arrange(columns: GraphColumn[], edges: GraphEdge[]): NodeBrief[][] {
  const columnOf = new Map<string, number>();
  columns.forEach((column, index) => column.nodes.forEach((node) => columnOf.set(node.id, index)));
  const neighbours = new Map<string, string[]>();
  for (const edge of edges) {
    if (!columnOf.has(edge.source) || !columnOf.has(edge.target)) continue;
    neighbours.set(edge.source, [...(neighbours.get(edge.source) ?? []), edge.target]);
    neighbours.set(edge.target, [...(neighbours.get(edge.target) ?? []), edge.source]);
  }
  const ordered = columns.map((column) =>
    [...column.nodes].sort((a, b) => displayName(a).localeCompare(displayName(b)) || a.id.localeCompare(b.id)),
  );
  const position = new Map<string, number>();
  const index = () => ordered.forEach((nodes) => nodes.forEach((node, row) => position.set(node.id, row)));
  index();

  const sweep = (from: number, to: number, step: number) => {
    for (let column = from; column !== to; column += step) {
      const reference = column - step;
      const weight = (node: NodeBrief): number => {
        const rows = (neighbours.get(node.id) ?? [])
          .filter((other) => columnOf.get(other) === reference)
          .map((other) => position.get(other) ?? 0);
        if (rows.length === 0) return position.get(node.id) ?? 0;
        return rows.reduce((sum, row) => sum + row, 0) / rows.length;
      };
      const weights = new Map(ordered[column].map((node) => [node.id, weight(node)]));
      ordered[column].sort(
        (a, b) => (weights.get(a.id) ?? 0) - (weights.get(b.id) ?? 0) || a.id.localeCompare(b.id),
      );
      ordered[column].forEach((node, row) => position.set(node.id, row));
    }
  };
  for (let pass = 0; pass < 2; pass += 1) {
    sweep(1, ordered.length, 1);
    sweep(ordered.length - 2, -1, -1);
  }
  return ordered;
}

function route(from: Placed, to: Placed): { path: string; midX: number; midY: number } {
  const y1 = from.y + BOX_H / 2;
  const y2 = to.y + BOX_H / 2;
  if (from.column === to.column) {
    // Same column: bow out to the right.
    const x = from.x + BOX_W;
    const bulge = 26 + Math.min(Math.abs(y2 - y1) * 0.18, 44);
    return {
      path: `M ${x} ${y1} C ${x + bulge} ${y1}, ${x + bulge} ${y2}, ${x + 4} ${y2}`,
      midX: x + bulge * 0.75,
      midY: (y1 + y2) / 2,
    };
  }
  const forward = to.column > from.column;
  const x1 = forward ? from.x + BOX_W : from.x;
  const x2 = forward ? to.x - 4 : to.x + BOX_W + 4;
  const bend = Math.max(Math.abs(x2 - x1) * 0.45, 30) * (forward ? 1 : -1);
  return {
    path: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`,
    midX: (x1 + x2) / 2,
    midY: (y1 + y2) / 2,
  };
}

/** Columns of nodes joined by typed, directed edges. Used for call graphs and data lineage. */
export function LayeredGraph({ columns, edges, rootId, selectedId, onSelect, ariaLabel }: Props) {
  const [hovered, setHovered] = useState<string | null>(null);

  const layout = useMemo(() => {
    const visible = columns.filter((column) => column.nodes.length > 0);
    const ordered = arrange(visible, edges);
    const tallest = Math.max(...ordered.map((nodes) => nodes.length), 1);
    const innerHeight = tallest * BOX_H + (tallest - 1) * GAP_Y;
    const placed = new Map<string, Placed>();
    ordered.forEach((nodes, column) => {
      const height = nodes.length * BOX_H + (nodes.length - 1) * GAP_Y;
      const top = PAD + HEADER + (innerHeight - height) / 2;
      nodes.forEach((node, row) => {
        placed.set(node.id, {
          node,
          column,
          x: PAD + column * (BOX_W + GAP_X),
          y: top + row * (BOX_H + GAP_Y),
        });
      });
    });

    // Fold runtime observations into the static edge they confirm; keep the rest as runtime-only.
    const statics = new Map<string, GraphEdge>();
    for (const edge of edges) {
      if (!edge.type.startsWith("OBSERVED")) statics.set(`${edge.source}|${edge.type}|${edge.target}`, edge);
    }
    const confirmed = new Map<string, { count: number; simulated: boolean }>();
    const drawable: GraphEdge[] = [];
    for (const edge of edges) {
      const counterpart = STATIC_OF[edge.type];
      if (counterpart) {
        const key = `${edge.source}|${counterpart}|${edge.target}`;
        if (statics.has(key)) {
          confirmed.set(key, { count: edge.count ?? 0, simulated: edge.agent === "simulated" });
          continue;
        }
      }
      drawable.push(edge);
    }
    const drawn: DrawnEdge[] = [];
    for (const edge of drawable) {
      const from = placed.get(edge.source);
      const to = placed.get(edge.target);
      if (!from || !to || from === to) continue;
      const key = `${edge.source}|${edge.type}|${edge.target}`;
      const seen = confirmed.get(key);
      drawn.push({
        key,
        edge,
        ...route(from, to),
        observed: seen ? seen.count : null,
        simulated: seen ? seen.simulated : edge.agent === "simulated",
      });
    }
    return {
      visible,
      placed,
      drawn,
      width: PAD * 2 + visible.length * BOX_W + Math.max(visible.length - 1, 0) * GAP_X + 48,
      height: PAD * 2 + HEADER + innerHeight,
    };
  }, [columns, edges]);

  const focus = hovered ?? null;
  const related = useMemo(() => {
    if (!focus) return null;
    const ids = new Set<string>([focus]);
    for (const { edge } of layout.drawn) {
      if (edge.source === focus) ids.add(edge.target);
      if (edge.target === focus) ids.add(edge.source);
    }
    return ids;
  }, [focus, layout.drawn]);

  if (layout.visible.length === 0) return null;

  return (
    <ScrollArea contentKey={`${layout.width}x${layout.height}`}>
      <svg
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        role="img"
        aria-label={ariaLabel}
        className="block select-none"
        style={{
          width: "100%",
          minWidth: Math.round(layout.width * MIN_SCALE),
          maxWidth: layout.width,
          height: "auto",
          aspectRatio: `${layout.width} / ${layout.height}`,
        }}
      >
        <defs>
          {(Object.keys(STROKE) as StrokeKind[]).map((kind) => (
            <marker
              key={kind}
              id={`arrow-${kind}`}
              viewBox="0 0 8 8"
              refX="7"
              refY="4"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 0.5 L 7.5 4 L 0 7.5 z" fill={STROKE[kind]} />
            </marker>
          ))}
        </defs>

        {layout.visible.map((column, index) => (
          <text
            key={column.id}
            x={PAD + index * (BOX_W + GAP_X)}
            y={PAD + 10}
            className="fill-faint text-[11px] font-semibold uppercase tracking-wider"
          >
            {column.title}
          </text>
        ))}

        <g fill="none">
          {layout.drawn.map(({ key, edge, path, observed, simulated, midX, midY }) => {
            const kind = strokeKind(edge.type);
            const active = !focus || edge.source === focus || edge.target === focus;
            const uncertain = edge.type === "CALLS" && edge.confidence < 0.8;
            return (
              <g key={key} opacity={active ? 1 : 0.12}>
                <path
                  d={path}
                  stroke={STROKE[kind]}
                  strokeWidth={kind === "contains" ? 1 : 1.5}
                  strokeDasharray={
                    kind === "contains" ? "2 4" : kind === "read" ? "5 3" : uncertain || kind === "runtime" ? "4 4" : undefined
                  }
                  markerEnd={`url(#arrow-${kind})`}
                >
                  <title>{describe(edge, observed, simulated)}</title>
                </path>
                {observed !== null ? (
                  <g>
                    <rect
                      x={midX - 13}
                      y={midY - 7}
                      width={26}
                      height={14}
                      rx={7}
                      fill="#10131a"
                      stroke={STROKE.runtime}
                      strokeWidth={1}
                    />
                    <text
                      x={midX}
                      y={midY + 3.5}
                      textAnchor="middle"
                      fill={STROKE.runtime}
                      className="text-[9px] font-semibold tabular-nums"
                    >
                      {observed > 999 ? "999+" : observed}
                      <title>{describe(edge, observed, simulated)}</title>
                    </text>
                  </g>
                ) : null}
              </g>
            );
          })}
        </g>

        {[...layout.placed.values()].map(({ node, x, y }) => {
          const tone = toneOf(node.label, node.kind);
          const colour = TONE_HEX[tone];
          const isRoot = node.id === rootId;
          const isSelected = node.id === selectedId;
          const dimmed = related !== null && !related.has(node.id);
          const subtitle = node.http_method
            ? `${node.http_method} ${node.route ?? ""}`.trim()
            : (node.location ?? labelName(node.label, node.kind));
          return (
            <g
              key={node.id}
              transform={`translate(${x} ${y})`}
              opacity={dimmed ? 0.3 : 1}
              className="cursor-pointer"
              role="button"
              tabIndex={0}
              aria-label={`${labelName(node.label, node.kind)} ${displayName(node)}`}
              onClick={() => onSelect(node.id)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onSelect(node.id);
                }
              }}
              onMouseEnter={() => setHovered(node.id)}
              onMouseLeave={() => setHovered(null)}
              onFocus={() => setHovered(node.id)}
              onBlur={() => setHovered(null)}
            >
              <title>{`${displayName(node)}${node.signature ? `\n${node.signature}` : ""}`}</title>
              <rect
                width={BOX_W}
                height={BOX_H}
                rx={8}
                fill={isRoot ? "#1c212c" : "#131720"}
                stroke={isSelected || isRoot ? colour : "#2a3140"}
                strokeWidth={isSelected ? 2 : 1}
              />
              <rect x={0} y={8} width={3} height={BOX_H - 16} rx={1.5} fill={colour} />
              <text x={12} y={17} className="fill-fg font-mono text-[12px]">
                {truncate(displayName(node), 26)}
              </text>
              <text x={12} y={31} className="fill-faint text-[10px]">
                {truncate(subtitle, 33)}
              </text>
              {node.dead_candidate ? <circle cx={BOX_W - 10} cy={10} r={3} fill={TONE_HEX.danger} /> : null}
              {node.is_entry_point ? <circle cx={BOX_W - 10} cy={10} r={3} fill={TONE_HEX.hist} /> : null}
            </g>
          );
        })}
      </svg>
    </ScrollArea>
  );
}

export function GraphLegend({ runtime = false, lineage = false }: { runtime?: boolean; lineage?: boolean }) {
  const items: { label: string; colour: string; dash?: string }[] = [
    { label: "calls", colour: STROKE.call },
    { label: "possible call", colour: STROKE.call, dash: "4 4" },
    { label: "reads table", colour: STROKE.read, dash: "5 3" },
    { label: "writes table", colour: STROKE.write },
  ];
  if (lineage) {
    items.push({ label: "fires trigger", colour: STROKE.trigger }, { label: "runs SQL", colour: STROKE.contains, dash: "2 4" });
  }
  if (runtime) items.push({ label: "seen at runtime only", colour: STROKE.runtime, dash: "4 4" });
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-dim">
      {items.map((item) => (
        <li key={item.label} className="flex items-center gap-1.5">
          <svg width="22" height="6" aria-hidden="true">
            <line x1="0" y1="3" x2="22" y2="3" stroke={item.colour} strokeWidth="1.6" strokeDasharray={item.dash} />
          </svg>
          {item.label}
        </li>
      ))}
      {runtime ? (
        <li className="flex items-center gap-1.5">
          <span className="rounded-full border border-runtime px-1.5 text-[9px] font-semibold leading-[13px] text-runtime">
            12
          </span>
          times seen at runtime
        </li>
      ) : null}
    </ul>
  );
}
