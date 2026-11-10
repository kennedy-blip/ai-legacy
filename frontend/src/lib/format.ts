import type { NodeBrief, NodeLabel } from "./types";

const numberFormat = new Intl.NumberFormat("en-US");

export function formatNumber(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : numberFormat.format(value);
}

/** "2019-10-21" from an ISO timestamp; the graph stores UTC. */
export function formatDate(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 10) : "–";
}

export function formatYear(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 4) : "–";
}

export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  return value >= 100 ? `${Math.round(value)} ms` : `${value.toFixed(1)} ms`;
}

export function fileName(path: string | null | undefined): string {
  if (!path) return "";
  const cut = path.lastIndexOf("/");
  return cut < 0 ? path : path.slice(cut + 1);
}

export function shortHash(hash: string | null | undefined): string {
  return hash ? hash.slice(0, 8) : "";
}

/** The name a person would use for the node. */
export function displayName(node: Pick<NodeBrief, "name" | "qualified_name" | "label" | "message">): string {
  if (node.label === "Commit") return node.message ? `${node.name} ${node.message}` : node.name;
  return node.qualified_name || node.name;
}

export type Tone = "fn" | "cls" | "tbl" | "qry" | "hist" | "danger" | "runtime" | "dim";

export function toneOf(label: NodeLabel | string, kind?: string): Tone {
  switch (label) {
    case "Function":
      return kind === "trigger" || kind === "procedure" ? "danger" : "fn";
    case "Class":
      return "cls";
    case "DatabaseTable":
      return "tbl";
    case "DatabaseQuery":
      return "qry";
    case "Commit":
    case "Developer":
      return "hist";
    default:
      return "dim";
  }
}

/** Hex colours for SVG, mirroring the theme tokens in globals.css. */
export const TONE_HEX: Record<Tone, string> = {
  fn: "#6ab0f3",
  cls: "#b39dfa",
  tbl: "#46d39a",
  qry: "#38c8c0",
  hist: "#e5a444",
  danger: "#f47174",
  runtime: "#fb9a4b",
  dim: "#9aa3b2",
};

export const TONE_TEXT: Record<Tone, string> = {
  fn: "text-fn",
  cls: "text-cls",
  tbl: "text-tbl",
  qry: "text-qry",
  hist: "text-hist",
  danger: "text-danger",
  runtime: "text-runtime",
  dim: "text-dim",
};

export function labelName(label: NodeLabel | string, kind?: string): string {
  switch (label) {
    case "Function":
      return kind && kind !== "function" ? kind : "function";
    case "Class":
      return kind && kind !== "class" ? kind : "class";
    case "DatabaseTable":
      return kind === "view" ? "view" : "table";
    case "DatabaseQuery":
      return "SQL";
    case "File":
      return "file";
    case "Commit":
      return "commit";
    case "Developer":
      return "developer";
    default:
      return String(label).toLowerCase();
  }
}

export function truncate(text: string, limit: number): string {
  return text.length <= limit ? text : `${text.slice(0, Math.max(limit - 1, 1))}…`;
}

export function plural(count: number, singular: string, pluralForm?: string): string {
  return `${formatNumber(count)} ${count === 1 ? singular : (pluralForm ?? `${singular}s`)}`;
}
