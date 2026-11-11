"use client";

import { useMemo, useState } from "react";

import { formatNumber, plural, TONE_HEX, truncate } from "@/lib/format";
import type { Topology, TopologyGroup, TopologyLink } from "@/lib/types";
import { ScrollArea } from "@/ui/common/ScrollArea";

interface Props {
  topology: Topology;
  /** Called with the node id of a table when it is clicked. */
  onSelectTable: (nodeId: string) => void;
}

const DIR_W = 270;
const TABLE_W = 190;
const BOX_H = 46;
const GAP_Y = 12;
const GUTTER = 150; // room on the left for arcs between directories
const SPAN = 260; // distance between the two columns
const PAD = 16;
const HEADER = 26;

const LINK_COLOUR: Record<string, string> = {
  calls: "#6f7b91",
  reads: TONE_HEX.qry,
  writes: TONE_HEX.tbl,
  triggers: TONE_HEX.danger,
};

function commonPrefix(paths: string[]): string {
  if (paths.length < 2) return "";
  const split = paths.map((path) => path.split("/"));
  const shortest = Math.min(...split.map((parts) => parts.length));
  let shared = 0;
  while (shared < shortest - 1 && split.every((parts) => parts[shared] === split[0][shared])) shared += 1;
  return shared === 0 ? "" : `${split[0].slice(0, shared).join("/")}/`;
}

function linkTitle(link: TopologyLink, names: Map<string, string>): string {
  const verb = { calls: "calls", reads: "reads", writes: "writes to", triggers: "fires a trigger in" }[link.kind] ?? link.kind;
  const seen = link.observed > 0 ? ` · seen ${formatNumber(link.observed)} times at runtime` : "";
  return `${names.get(link.source) ?? link.source} ${verb} ${names.get(link.target) ?? link.target} · ${link.weight} static ${
    link.weight === 1 ? "link" : "links"
  }${seen}`;
}

/** Architecture at a glance: code grouped by directory on the left, tables on the right. */
export function TopologyMap({ topology, onSelectTable }: Props) {
  const [hovered, setHovered] = useState<string | null>(null);

  const layout = useMemo(() => {
    const directories = topology.groups
      .filter((group) => group.kind !== "table")
      .sort((a, b) => a.name.localeCompare(b.name));
    const tables = topology.groups.filter((group) => group.kind === "table").sort((a, b) => a.name.localeCompare(b.name));
    // Deep trees share a long prefix; show it once instead of on every box.
    const javaLike = directories.filter((group) => group.name.split("/").length > 3).map((group) => group.name);
    const prefix = commonPrefix(javaLike);
    const rows = Math.max(directories.length, tables.length, 1);
    const inner = rows * BOX_H + (rows - 1) * GAP_Y;
    const position = new Map<string, { x: number; y: number; width: number }>();
    const place = (groups: TopologyGroup[], x: number, width: number) => {
      const height = groups.length * BOX_H + Math.max(groups.length - 1, 0) * GAP_Y;
      const top = PAD + HEADER + (inner - height) / 2;
      groups.forEach((group, row) => position.set(group.id, { x, y: top + row * (BOX_H + GAP_Y), width }));
    };
    place(directories, PAD + GUTTER, DIR_W);
    place(tables, PAD + GUTTER + DIR_W + SPAN, TABLE_W);
    const maxLines = Math.max(...directories.map((group) => group.lines), 1);
    return {
      directories,
      tables,
      prefix,
      position,
      maxLines,
      width: PAD * 2 + GUTTER + DIR_W + SPAN + TABLE_W,
      height: PAD * 2 + HEADER + inner,
    };
  }, [topology]);

  const names = useMemo(() => new Map(topology.groups.map((group) => [group.id, group.name])), [topology]);
  const maxWeight = Math.max(...topology.links.map((link) => link.weight), 1);

  const path = (link: TopologyLink): string | null => {
    const from = layout.position.get(link.source);
    const to = layout.position.get(link.target);
    if (!from || !to) return null;
    const y1 = from.y + BOX_H / 2;
    const y2 = to.y + BOX_H / 2;
    if (from.x === to.x) {
      // Directory to directory: an arc through the left gutter.
      const bulge = Math.min(34 + Math.abs(y2 - y1) * 0.32, GUTTER - 10);
      return `M ${from.x} ${y1} C ${from.x - bulge} ${y1}, ${to.x - bulge} ${y2}, ${to.x - 4} ${y2}`;
    }
    const forward = to.x > from.x;
    const x1 = forward ? from.x + from.width : from.x;
    const x2 = forward ? to.x - 4 : to.x + to.width + 4;
    const bend = (x2 - x1) * 0.5;
    return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
  };

  const touches = (link: TopologyLink) => hovered === null || link.source === hovered || link.target === hovered;
  const connected = useMemo(() => {
    if (hovered === null) return null;
    const ids = new Set<string>([hovered]);
    for (const link of topology.links) {
      if (link.source === hovered) ids.add(link.target);
      if (link.target === hovered) ids.add(link.source);
    }
    return ids;
  }, [hovered, topology.links]);

  const box = (group: TopologyGroup) => {
    const place = layout.position.get(group.id);
    if (!place) return null;
    const isTable = group.kind === "table";
    const colour = isTable ? TONE_HEX.tbl : TONE_HEX.fn;
    const name = !isTable && layout.prefix && group.name.startsWith(layout.prefix)
      ? `…/${group.name.slice(layout.prefix.length)}`
      : group.name;
    const dimmed = connected !== null && !connected.has(group.id);
    const languages = Object.keys(group.languages).join(", ");
    const detail = isTable
      ? "table"
      : `${plural(group.files, "file")} · ${plural(group.functions, "function")} · ${plural(group.lines, "line")}`;
    return (
      <g
        key={group.id}
        transform={`translate(${place.x} ${place.y})`}
        opacity={dimmed ? 0.3 : 1}
        className={isTable ? "cursor-pointer" : undefined}
        role={isTable ? "button" : undefined}
        tabIndex={0}
        aria-label={`${isTable ? "table" : "directory"} ${group.name}`}
        onClick={isTable ? () => onSelectTable(group.id) : undefined}
        onKeyDown={
          isTable
            ? (event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onSelectTable(group.id);
                }
              }
            : undefined
        }
        onMouseEnter={() => setHovered(group.id)}
        onMouseLeave={() => setHovered(null)}
        onFocus={() => setHovered(group.id)}
        onBlur={() => setHovered(null)}
      >
        <title>{`${group.name}\n${detail}${languages ? `\n${languages}` : ""}${
          group.entry_points ? `\n${group.entry_points} entry points` : ""
        }`}</title>
        <rect width={place.width} height={BOX_H} rx={8} fill="#131720" stroke="#2a3140" />
        <rect x={0} y={9} width={3} height={BOX_H - 18} rx={1.5} fill={colour} />
        <text x={12} y={19} className="fill-fg font-mono text-[12px]">
          {truncate(name, isTable ? 24 : 34)}
        </text>
        <text x={12} y={34} className="fill-faint text-[10px]">
          {truncate(detail, 44)}
        </text>
        {!isTable ? (
          <rect
            x={12}
            y={BOX_H - 5}
            width={Math.max(((place.width - 24) * group.lines) / layout.maxLines, 2)}
            height={2}
            rx={1}
            fill={colour}
            opacity={0.55}
          />
        ) : null}
        {group.entry_points > 0 ? (
          <circle cx={place.width - 10} cy={10} r={3} fill={TONE_HEX.hist} />
        ) : null}
      </g>
    );
  };

  return (
    <ScrollArea contentKey={`${layout.width}x${layout.height}`}>
      <svg
        width={layout.width}
        height={layout.height}
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        role="img"
        aria-label={`Topology: ${layout.directories.length} code groups and ${layout.tables.length} tables`}
        className="block select-none"
      >
        <defs>
          {Object.entries(LINK_COLOUR).map(([kind, colour]) => (
            <marker
              key={kind}
              id={`topo-arrow-${kind}`}
              viewBox="0 0 8 8"
              refX="7"
              refY="4"
              markerWidth="6"
              markerHeight="6"
              orient="auto"
            >
              <path d="M 0 0.5 L 7.5 4 L 0 7.5 z" fill={colour} />
            </marker>
          ))}
        </defs>
        <text x={PAD + GUTTER} y={PAD + 10} className="fill-faint text-[11px] font-semibold uppercase tracking-wider">
          Code, by directory{layout.prefix ? ` (… = ${layout.prefix})` : ""}
        </text>
        <text
          x={PAD + GUTTER + DIR_W + SPAN}
          y={PAD + 10}
          className="fill-faint text-[11px] font-semibold uppercase tracking-wider"
        >
          Tables
        </text>
        <g fill="none">
          {topology.links.map((link) => {
            const d = path(link);
            if (!d) return null;
            const colour = LINK_COLOUR[link.kind] ?? TONE_HEX.dim;
            return (
              <path
                key={`${link.source}|${link.kind}|${link.target}`}
                d={d}
                stroke={colour}
                strokeWidth={1 + (2.2 * Math.log2(1 + link.weight)) / Math.log2(1 + maxWeight)}
                strokeDasharray={link.kind === "reads" ? "5 3" : undefined}
                opacity={touches(link) ? 0.9 : 0.08}
                markerEnd={`url(#topo-arrow-${link.kind in LINK_COLOUR ? link.kind : "calls"})`}
              >
                <title>{linkTitle(link, names)}</title>
              </path>
            );
          })}
        </g>
        {layout.directories.map(box)}
        {layout.tables.map(box)}
      </svg>
    </ScrollArea>
  );
}

export function TopologyLegend() {
  const items = [
    { label: "calls", colour: LINK_COLOUR.calls },
    { label: "reads", colour: LINK_COLOUR.reads, dash: "5 3" },
    { label: "writes", colour: LINK_COLOUR.writes },
    { label: "trigger", colour: LINK_COLOUR.triggers },
  ];
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
      <li>line width grows with the number of links</li>
      <li className="flex items-center gap-1.5">
        <span className="size-1.5 rounded-full bg-hist" /> contains entry points
      </li>
    </ul>
  );
}
