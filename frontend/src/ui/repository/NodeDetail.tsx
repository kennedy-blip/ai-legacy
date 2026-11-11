"use client";

import type { ReactNode } from "react";

import { api } from "@/lib/api";
import { displayName, formatDate, labelName, plural, toneOf } from "@/lib/format";
import { useResource } from "@/lib/hooks";
import type { GraphEdge, Neighbour, NodeBrief, NodeContext, NodeFull } from "@/lib/types";
import { Badge, Centered, Empty, ErrorNote, Section, Spinner } from "@/ui/common/primitives";
import { CallGraph } from "@/ui/visualizers/CallGraph";
import { GraphLegend } from "@/ui/visualizers/LayeredGraph";

import { useWorkspace } from "./context";
import { NodeFlags, NodeRow } from "./NodeRef";
import { SourceViewer } from "./SourceViewer";

/** Everything known about one node: what it is, its source, its neighbours and its history. */
export function NodeDetail({ nodeId }: { nodeId: string }) {
  const { repositoryId, open, ask } = useWorkspace();
  const context = useResource(`node:${repositoryId}:${nodeId}`, (signal) => api.node(repositoryId, nodeId, signal));
  const node = context.data?.node;
  const hasSource = Boolean(node?.path && (node.line || node.start_line || node.label === "File"));
  const source = useResource(hasSource ? `source:${repositoryId}:${nodeId}` : null, (signal) =>
    api.nodeSource(repositoryId, nodeId, signal),
  );
  const graphable = node?.label === "Function" || node?.label === "DatabaseTable";
  const chain = useResource(graphable ? `chain:${repositoryId}:${nodeId}` : null, (signal) =>
    api.callChain(repositoryId, nodeId, { depth: 2, runtime: true }, signal),
  );

  if (context.error) {
    return context.error.status === 404 ? (
      <Empty title="This node is no longer in the graph">
        The repository may have been analysed again since the link was made.
      </Empty>
    ) : (
      <ErrorNote error={context.error} onRetry={context.reload} />
    );
  }
  if (!context.data || !node) {
    return (
      <Centered>
        <Spinner />
      </Centered>
    );
  }
  const data = context.data;
  const tone = toneOf(node.label, node.kind);
  const name = displayName(node);

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <header className="panel p-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={tone}>{labelName(node.label, node.kind)}</Badge>
          {node.language ? <Badge>{node.language}</Badge> : null}
          <NodeFlags node={node} />
          {node.declared === false ? (
            <Badge tone="runtime" title="Used in SQL, but no CREATE TABLE was found in this repository">
              not declared here
            </Badge>
          ) : null}
        </div>
        <h2 className="mt-2 break-all font-mono text-lg font-medium">
          {node.label === "Commit" ? node.name : name}
        </h2>
        {node.label === "Commit" ? <p className="mt-1 text-[15px]">{node.message}</p> : null}
        {node.signature && node.signature !== name ? (
          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words font-mono text-[13px] text-dim">
            {node.signature}
          </pre>
        ) : null}
        {node.docstring ? <p className="mt-2 max-w-3xl text-sm leading-relaxed text-dim">{node.docstring}</p> : null}
        <Facts node={node} context={data} />
        <div className="mt-3 flex flex-wrap gap-2">
          {node.label === "Function" ? (
            <button type="button" className="btn" onClick={() => open(node.id, "flow")}>
              Sequence diagram
            </button>
          ) : null}
          {node.label === "Function" || node.label === "DatabaseTable" || node.label === "DatabaseQuery" ? (
            <button type="button" className="btn" onClick={() => open(node.id, "lineage")}>
              Data lineage
            </button>
          ) : null}
          {node.label !== "Developer" ? (
            <button type="button" className="btn btn-primary" onClick={() => ask(questionFor(node), node.id)}>
              Ask about this
            </button>
          ) : null}
          {node.pr_url ? (
            <a className="btn" href={node.pr_url} target="_blank" rel="noreferrer">
              Pull request #{node.pr_number}
            </a>
          ) : null}
        </div>
      </header>

      {node.label === "Commit" && typeof node.body === "string" && node.body ? (
        <Section title="Commit message">
          <pre className="whitespace-pre-wrap break-words p-4 font-sans text-sm leading-relaxed text-dim">{node.body}</pre>
        </Section>
      ) : null}

      {node.ddl ? (
        <Section title="Definition" aside={node.columns ? plural(node.columns.length, "column") : undefined}>
          <pre className="overflow-x-auto p-4 font-mono text-[12.5px] leading-relaxed text-dim">{node.ddl}</pre>
        </Section>
      ) : null}

      {node.sql ? (
        <Section title="SQL" aside={node.dynamic ? "assembled at runtime: ${…} marks non-literal parts" : undefined}>
          <pre className="overflow-x-auto whitespace-pre-wrap break-words p-4 font-mono text-[12.5px] leading-relaxed text-qry">
            {node.sql}
          </pre>
        </Section>
      ) : null}

      {hasSource ? (
        source.data ? (
          <SourceViewer slice={source.data} />
        ) : source.error ? (
          <p className="text-sm text-faint">
            {source.error.status === 404 || source.error.status === 409
              ? "The source text is not available on this server; the graph facts below are complete."
              : source.error.message}
          </p>
        ) : (
          <Spinner label="Loading source" />
        )
      ) : null}

      {graphable ? (
        <Section title="Neighbourhood" aside="two hops in each direction, runtime observations overlaid">
          {chain.data ? (
            chain.data.nodes.length > 1 ? (
              <div className="flex flex-col gap-3 p-3">
                <CallGraph chain={chain.data} selectedId={nodeId} onSelect={(id) => open(id, "explore")} />
                <GraphLegend runtime />
                {chain.data.truncated ? (
                  <p className="text-xs text-faint">The graph was cut at its node limit; not every neighbour is drawn.</p>
                ) : null}
              </div>
            ) : (
              <Empty title="No resolved caller or callee">
                {node.label === "Function"
                  ? "Static analysis found nothing calling this and nothing it calls inside the repository."
                  : "No code in this repository reads or writes this table."}
              </Empty>
            )
          ) : chain.error ? (
            <div className="p-3">
              <ErrorNote error={chain.error} onRetry={chain.reload} />
            </div>
          ) : (
            <Centered>
              <Spinner />
            </Centered>
          )}
        </Section>
      ) : null}

      <div className="grid items-start gap-4 xl:grid-cols-2">
        <Relations title="Called by" items={data.callers} trailing={callInfo} empty={callersEmpty(node)} />
        <Relations
          title="Calls"
          items={data.callees}
          trailing={callInfo}
          extra={node.unresolved_calls?.length ? <Unresolved node={node} /> : undefined}
        />
        <Relations title="Reads" items={data.reads} trailing={dataInfo} />
        <Relations title="Writes" items={data.writes} trailing={dataInfo} />
        <Relations title="Read by" items={data.readers} trailing={dataInfo} />
        <Relations title="Written by" items={data.writers} trailing={dataInfo} />
        <Relations
          title={node.label === "DatabaseTable" ? "Triggers fired by writes" : "Fired by"}
          items={data.triggers}
          trailing={(edge) => edge.events?.join(", ")}
        />
        <Relations title="Extends / implements" items={data.supertypes} />
        <Relations title="Extended by" items={data.subtypes} />
        <Relations
          title="Seen at runtime calling"
          items={data.observed_callees}
          trailing={(edge) => observedInfo(edge)}
        />
        <Relations
          title="Seen at runtime called by"
          items={data.observed_callers}
          trailing={(edge) => observedInfo(edge)}
        />
        <NodeList title="SQL statements" nodes={data.queries} />
        <NodeList title={node.label === "File" ? "Defined in this file" : "Members"} nodes={data.members} />
        <Relations
          title="History"
          aside="commits that still own lines here"
          items={data.commits}
          trailing={(edge, neighbour) =>
            `${formatDate(neighbour.authored_at)}${typeof edge.lines === "number" ? ` · ${plural(edge.lines, "line")}` : ""}`
          }
          subtitle={(neighbour) => `${neighbour.author_name ?? "unknown"} · ${neighbour.message ?? ""}`}
        />
        <Relations
          title="Code this commit still owns"
          items={data.touched}
          trailing={(edge) => (typeof edge.lines === "number" ? plural(edge.lines, "line") : null)}
        />
      </div>
    </div>
  );
}

function questionFor(node: NodeFull): string {
  const name = displayName(node);
  switch (node.label) {
    case "DatabaseTable":
      return `Which code reads and writes ${name}, and are there triggers or other hidden side effects?`;
    case "Commit":
      return `What did commit ${node.name} change, and why?`;
    case "Class":
      return `What is ${name} responsible for, and who depends on it?`;
    case "File":
      return `What does ${name} contain, and how is it used?`;
    case "DatabaseQuery":
      return `What does the statement ${name} do, and which code runs it?`;
    default:
      return `What does ${name} do, step by step, and why was it written this way?`;
  }
}

function callersEmpty(node: NodeFull): string | undefined {
  if (node.label !== "Function") return undefined;
  if (node.is_entry_point) return "None inside the repository: this is an entry point, called by a framework or a scheduler.";
  return "No caller found. Reflection, dependency injection and callers outside this repository are not visible to static analysis.";
}

function callInfo(edge: GraphEdge): ReactNode {
  const lines = Array.isArray(edge.lines) && edge.lines.length ? `line ${edge.lines.join(", ")}` : null;
  return (
    <span className="flex items-center gap-1.5">
      {edge.confidence < 0.8 ? (
        <Badge tone="runtime" title={`Resolved by ${edge.resolution ?? "name"}: ${Math.round(edge.confidence * 100)}% confidence`}>
          possible
        </Badge>
      ) : null}
      {lines}
    </span>
  );
}

function dataInfo(edge: GraphEdge): ReactNode {
  const seen = edge.type.startsWith("OBSERVED");
  return (
    <span className="flex items-center gap-1.5">
      {seen ? <Badge tone="runtime">{edge.agent === "simulated" ? "simulated" : "runtime"}</Badge> : null}
      {edge.operations?.join(", ")}
    </span>
  );
}

function observedInfo(edge: GraphEdge): ReactNode {
  return (
    <span className="flex items-center gap-1.5">
      {edge.agent === "simulated" ? <Badge tone="runtime">simulated</Badge> : null}
      {plural(edge.count ?? 0, "time")}
    </span>
  );
}

function Unresolved({ node }: { node: NodeFull }) {
  const names = node.unresolved_calls ?? [];
  if (names.length === 0) return null;
  return (
    <p className="border-t border-line px-3 py-2 text-xs leading-relaxed text-faint">
      Also calls outside the repository (libraries or unresolved):{" "}
      <span className="font-mono text-dim">{names.slice(0, 12).join(", ")}</span>
      {names.length > 12 ? ` and ${names.length - 12} more` : ""}
    </p>
  );
}

function Facts({ node, context }: { node: NodeFull; context: NodeContext }) {
  const facts: [string, ReactNode][] = [];
  if (node.location) facts.push(["location", node.location]);
  if (node.label === "Function") {
    if (node.loc) facts.push(["size", plural(node.loc, "line")]);
    if (node.complexity) facts.push(["branches", node.complexity]);
    facts.push(["callers", context.summary.callers ?? 0], ["callees", context.summary.callees ?? 0]);
  }
  if (node.label === "Commit") {
    facts.push(["author", node.author_name ?? "unknown"], ["date", formatDate(node.authored_at)]);
    if (node.tickets?.length) facts.push(["tickets", node.tickets.join(", ")]);
  } else if (node.last_modified_at) {
    facts.push(["last change", `${formatDate(node.last_modified_at)} by ${node.last_author ?? "unknown"}`]);
    if (node.authors?.length) facts.push(["line ownership", node.authors.join(", ")]);
  }
  if (facts.length === 0) return null;
  return (
    <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs">
      {facts.map(([label, value]) => (
        <div key={label} className="flex gap-1.5">
          <dt className="text-faint">{label}</dt>
          <dd className="font-mono text-dim">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Relations({
  title,
  aside,
  items,
  trailing,
  subtitle,
  empty,
  extra,
}: {
  title: string;
  aside?: string;
  items: Neighbour[];
  trailing?: (edge: GraphEdge, node: NodeBrief) => ReactNode;
  subtitle?: (node: NodeBrief) => string;
  empty?: string;
  extra?: ReactNode;
}) {
  if (items.length === 0 && !empty && !extra) return null;
  return (
    <Section title={title} aside={aside ?? (items.length ? String(items.length) : undefined)}>
      {items.length > 0 ? (
        <ul className="max-h-80 divide-y divide-line overflow-auto">
          {items.map(({ node, edge }) => (
            <li key={edge.id}>
              <NodeRow node={node} trailing={trailing?.(edge, node)} subtitle={subtitle?.(node)} />
            </li>
          ))}
        </ul>
      ) : empty ? (
        <p className="px-3 py-3 text-sm text-dim">{empty}</p>
      ) : null}
      {extra}
    </Section>
  );
}

function NodeList({ title, nodes }: { title: string; nodes: NodeBrief[] }) {
  if (nodes.length === 0) return null;
  return (
    <Section title={title} aside={String(nodes.length)}>
      <ul className="max-h-80 divide-y divide-line overflow-auto">
        {nodes.map((node) => (
          <li key={node.id}>
            <NodeRow node={node} />
          </li>
        ))}
      </ul>
    </Section>
  );
}
