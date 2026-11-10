"use client";

import { useState } from "react";

import { TONE_HEX, truncate } from "@/lib/format";
import type { Sequence, SequenceParticipant, SequenceStep } from "@/lib/types";

interface Props {
  sequence: Sequence;
  /** Called with the node a step leads to (a function or a table). */
  onSelect: (nodeId: string) => void;
}

const COLUMN = 150;
const HEAD_W = 134;
const HEAD_H = 40;
const ROW = 34;
const PAD_X = 44;
const PAD_Y = 12;

const KIND_COLOUR: Record<string, string> = {
  call: TONE_HEX.fn,
  read: TONE_HEX.qry,
  write: TONE_HEX.tbl,
  trigger: TONE_HEX.danger,
};

function participantColour(participant: SequenceParticipant): string {
  if (participant.kind === "table") return TONE_HEX.tbl;
  if (participant.kind === "routine") return TONE_HEX.danger;
  return TONE_HEX.cls;
}

function stepTitle(step: SequenceStep): string {
  const where = step.path ? `${step.path.slice(step.path.lastIndexOf("/") + 1)}${step.line ? `:${step.line}` : ""}` : "";
  const sure = step.confidence < 0.8 ? ` · possible (${Math.round(step.confidence * 100)}% confidence)` : "";
  return `${step.index}. ${step.kind} ${step.label}${where ? ` · ${where}` : ""}${sure}${step.note ? ` · ${step.note}` : ""}`;
}

/**
 * Static sequence diagram: what a function does, in source order, down to the tables
 * it touches. It shows every call the code can make, not one particular execution.
 */
export function SequenceDiagram({ sequence, onSelect }: Props) {
  const [hovered, setHovered] = useState<number | null>(null);
  const { participants, steps } = sequence;
  const column = new Map(participants.map((participant, index) => [participant.id, index]));
  const centre = (id: string) => PAD_X + (column.get(id) ?? 0) * COLUMN + HEAD_W / 2;

  const top = PAD_Y + HEAD_H;
  const bodyHeight = (steps.length + 1) * ROW;
  const width = PAD_X * 2 + Math.max(participants.length - 1, 0) * COLUMN + HEAD_W;
  const height = top + bodyHeight + HEAD_H + PAD_Y;

  const heads = (y: number, keyPrefix: string) =>
    participants.map((participant, index) => {
      const colour = participantColour(participant);
      return (
        <g key={`${keyPrefix}-${participant.id}`} transform={`translate(${PAD_X + index * COLUMN} ${y})`}>
          <title>{`${participant.kind} ${participant.name}${participant.path ? `\n${participant.path}` : ""}`}</title>
          <rect width={HEAD_W} height={HEAD_H} rx={8} fill="#131720" stroke={colour} strokeOpacity={0.6} />
          <text x={HEAD_W / 2} y={17} textAnchor="middle" className="fill-fg font-mono text-[12px]">
            {truncate(participant.name, 17)}
          </text>
          <text x={HEAD_W / 2} y={31} textAnchor="middle" fill={colour} className="text-[10px]">
            {participant.kind}
          </text>
        </g>
      );
    });

  return (
    <div className="overflow-auto">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`Sequence of ${sequence.root.qualified_name ?? sequence.root.name}: ${steps.length} steps across ${participants.length} participants`}
        className="block select-none"
      >
        <defs>
          {Object.entries(KIND_COLOUR).map(([kind, colour]) => (
            <marker
              key={kind}
              id={`seq-arrow-${kind}`}
              viewBox="0 0 8 8"
              refX="7.5"
              refY="4"
              markerWidth="7"
              markerHeight="7"
              orient="auto"
            >
              <path d="M 0 0.5 L 7.5 4 L 0 7.5 z" fill={colour} />
            </marker>
          ))}
        </defs>

        {participants.map((participant) => (
          <line
            key={participant.id}
            x1={centre(participant.id)}
            x2={centre(participant.id)}
            y1={top}
            y2={top + bodyHeight}
            stroke="#2a3140"
            strokeDasharray="3 5"
          />
        ))}
        {heads(PAD_Y, "top")}
        {heads(top + bodyHeight, "bottom")}

        {steps.map((step, index) => {
          const y = top + (index + 1) * ROW;
          const colour = KIND_COLOUR[step.kind] ?? TONE_HEX.dim;
          const marker = `url(#seq-arrow-${step.kind in KIND_COLOUR ? step.kind : "call"})`;
          const from = centre(step.source);
          const to = centre(step.target);
          const self = step.source === step.target;
          const dashed = step.kind === "read" || step.confidence < 0.8;
          const active = hovered === null || hovered === step.index;
          const labelX = self ? from + 44 : (from + to) / 2;
          const anchor = self ? "start" : "middle";
          const label = `${step.label}${step.line ? ` :${step.line}` : ""}`;
          return (
            <g
              key={step.index}
              opacity={active ? 1 : 0.35}
              className="cursor-pointer"
              role="button"
              tabIndex={0}
              aria-label={stepTitle(step)}
              onClick={() => onSelect(step.to_node)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onSelect(step.to_node);
                }
              }}
              onMouseEnter={() => setHovered(step.index)}
              onMouseLeave={() => setHovered(null)}
              onFocus={() => setHovered(step.index)}
              onBlur={() => setHovered(null)}
            >
              <title>{stepTitle(step)}</title>
              {/* A wide transparent band makes the thin arrow easy to hit. */}
              <rect x={0} y={y - ROW / 2} width={width} height={ROW} fill="transparent" />
              <text x={PAD_X - 12} y={y + 3.5} textAnchor="end" className="fill-faint text-[10px] tabular-nums">
                {step.index}
              </text>
              {self ? (
                <path
                  d={`M ${from} ${y - 7} h 34 v 14 h -28`}
                  fill="none"
                  stroke={colour}
                  strokeWidth={1.5}
                  strokeDasharray={dashed ? "5 3" : undefined}
                  markerEnd={marker}
                />
              ) : (
                <line
                  x1={from}
                  x2={to + (to > from ? -2 : 2)}
                  y1={y}
                  y2={y}
                  stroke={colour}
                  strokeWidth={step.kind === "trigger" ? 2 : 1.5}
                  strokeDasharray={dashed ? "5 3" : undefined}
                  markerEnd={marker}
                />
              )}
              <text
                x={labelX}
                y={y - 6}
                textAnchor={anchor}
                className="font-mono text-[11px]"
                fill={step.kind === "call" ? "#c9cfdb" : colour}
                stroke="#0a0c10"
                strokeWidth={3}
                paintOrder="stroke"
              >
                {truncate(label, self ? 22 : Math.max(Math.floor(Math.abs(to - from) / 7), 14))}
                {step.confidence < 0.8 ? " ?" : ""}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export function SequenceLegend() {
  const items = [
    { label: "call", colour: KIND_COLOUR.call },
    { label: "read", colour: KIND_COLOUR.read, dash: "5 3" },
    { label: "write", colour: KIND_COLOUR.write },
    { label: "trigger fired by the database", colour: KIND_COLOUR.trigger },
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
      <li>“?” marks a call resolved by name only</li>
    </ul>
  );
}
