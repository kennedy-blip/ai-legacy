"use client";

import { useState, type ReactNode } from "react";

import { api, ApiError } from "@/lib/api";
import { displayName, formatMs, formatNumber, plural } from "@/lib/format";
import { useResource } from "@/lib/hooks";
import type { NodeBrief, ObservedEdge, TelemetrySummary } from "@/lib/types";
import { Badge, Centered, ErrorNote, Section, Spinner, Stat } from "@/ui/common/primitives";

import { useWorkspace } from "./context";
import { NodeLink, NodeRow } from "./NodeRef";

/** What a running system reported, set against what the source code predicts. */
export function RuntimeTab() {
  const { repositoryId, system, refresh } = useWorkspace();
  const telemetry = useResource(`telemetry:${repositoryId}`, (signal) => api.telemetry(repositoryId, signal));
  const [busy, setBusy] = useState<"simulate" | "clear" | null>(null);
  const [failure, setFailure] = useState<ApiError | null>(null);
  const readOnly = system?.read_only ?? true;

  const run = async (action: "simulate" | "clear") => {
    setBusy(action);
    setFailure(null);
    try {
      if (action === "simulate") await api.simulate(repositoryId, 120);
      else await api.clearTelemetry(repositoryId);
      telemetry.reload();
      refresh();
    } catch (error) {
      setFailure(error instanceof ApiError ? error : new ApiError(0, "The request failed."));
    } finally {
      setBusy(null);
    }
  };

  const summary = telemetry.latest;
  if (telemetry.error && !summary) return <ErrorNote error={telemetry.error} onRetry={telemetry.reload} />;
  if (!summary) {
    return (
      <Centered>
        <Spinner label="Reading observations" />
      </Centered>
    );
  }
  const { totals } = summary;
  const empty = totals.observed_edges === 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold">Runtime overlay</h2>
          <p className="max-w-3xl text-sm text-dim">
            Static analysis says what the code can do. Traces from a running system say what it does. Laying
            one over the other shows calls the parser could not see and code that never runs.
          </p>
        </div>
        <button type="button" className="btn" onClick={telemetry.reload} disabled={telemetry.loading}>
          Refresh
        </button>
        {!readOnly ? (
          <>
            <button type="button" className="btn" disabled={busy !== null} onClick={() => void run("simulate")}>
              {busy === "simulate" ? "Simulating…" : "Simulate traffic"}
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy !== null || empty}
              onClick={() => {
                if (window.confirm("Remove every runtime observation for this repository?")) void run("clear");
              }}
            >
              {busy === "clear" ? "Clearing…" : "Clear"}
            </button>
          </>
        ) : null}
      </div>

      {failure ? <ErrorNote error={failure} /> : null}

      {summary.simulated ? (
        <p className="rounded-lg border border-runtime/40 bg-runtime/5 px-3 py-2 text-sm text-runtime">
          <span className="font-semibold">Simulated traffic.</span> These observations were produced by a
          generator that walks the static graph from its entry points. They show what the overlay looks like;
          they are not measurements of a real system and prove nothing about this code.
        </p>
      ) : null}

      {empty ? (
        <NoTelemetry repositoryId={repositoryId} readOnly={readOnly} />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <Stat label="Invocations" value={formatNumber(totals.invocations)} hint="Calls counted across all traces" />
            <Stat
              label="Static calls seen"
              value={`${formatNumber(totals.confirmed_static_calls)} / ${formatNumber(totals.static_calls)}`}
              hint="Call relationships found in source that also appeared in a trace"
            />
            <Stat
              label="Runtime only"
              value={formatNumber(totals.runtime_only_calls)}
              hint="Calls seen in traces that static analysis did not find (reflection, dynamic dispatch, injection)"
            />
            <Stat
              label="Never observed"
              value={formatNumber(totals.never_observed_functions)}
              hint="Functions that source says are called, but no trace reached"
            />
            <Stat
              label="Data access seen"
              value={formatNumber(totals.observed_data_access)}
              hint="Function to table reads and writes seen in traces"
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <EdgeList
              title="Busiest calls"
              aside="by number of invocations"
              edges={summary.top_calls}
              summary={summary}
              detail={(edge) => (
                <>
                  {!edge.in_static_graph ? <Badge tone="runtime">not in source graph</Badge> : null}
                  {edge.in_static_graph && edge.static_confidence !== null && edge.static_confidence < 0.8 ? (
                    <Badge tone="tbl" title={`Static analysis only guessed this call (${edge.static_resolution ?? "by name"}); a trace confirms it`}>
                      guess confirmed
                    </Badge>
                  ) : null}
                  <span className="tabular-nums">{formatNumber(edge.count ?? 0)}×</span>
                  <span className="w-16 text-right tabular-nums">{formatMs(edge.mean_ms)}</span>
                </>
              )}
            />
            <EdgeList
              title="Data access"
              aside="function to table"
              edges={summary.data_access}
              summary={summary}
              detail={(edge) => (
                <>
                  {!edge.in_static_graph ? <Badge tone="runtime">not in source graph</Badge> : null}
                  <span>{edge.operations?.join(", ")}</span>
                  <span className="tabular-nums">{formatNumber(edge.count ?? 0)}×</span>
                </>
              )}
            />
            <EdgeList
              title="Seen at runtime, missing from source analysis"
              aside={plural(totals.runtime_only_calls, "call")}
              edges={summary.runtime_only}
              summary={summary}
              empty="Every observed call was also found in the source. Nothing hidden behind reflection or dynamic dispatch showed up in these traces."
              detail={(edge) => <span className="tabular-nums">{formatNumber(edge.count ?? 0)}×</span>}
            />
            <Section title="Called in source, never observed" aside={plural(totals.never_observed_functions, "function")}>
              {summary.never_observed.length ? (
                <>
                  <ul className="max-h-80 divide-y divide-line overflow-auto">
                    {summary.never_observed.map((node) => (
                      <li key={node.id}>
                        <NodeRow node={node} />
                      </li>
                    ))}
                  </ul>
                  <p className="border-t border-line px-3 py-2 text-xs leading-relaxed text-faint">
                    Not proof of dead code: the traces may simply not cover these paths. Treat it as a list of
                    places to check.
                  </p>
                </>
              ) : (
                <p className="px-3 py-3 text-sm text-dim">Every function that source says is called appeared in a trace.</p>
              )}
            </Section>
          </div>
          <p className="text-xs text-faint">
            Reported by: {summary.sources.join(", ") || "unknown"}
            {summary.last_seen ? ` · last observation ${summary.last_seen.replace("T", " ").slice(0, 19)} UTC` : ""}
          </p>
        </>
      )}
    </div>
  );
}

function EdgeList({
  title,
  aside,
  edges,
  summary,
  detail,
  empty,
}: {
  title: string;
  aside?: string;
  edges: ObservedEdge[];
  summary: TelemetrySummary;
  detail: (edge: ObservedEdge) => ReactNode;
  empty?: string;
}) {
  if (edges.length === 0 && !empty) return null;
  const unknown = (id: string): NodeBrief => ({ id, label: "Function", name: id, location: null });
  return (
    <Section title={title} aside={aside}>
      {edges.length === 0 ? (
        <p className="px-3 py-3 text-sm text-dim">{empty}</p>
      ) : (
        <ul className="max-h-96 divide-y divide-line overflow-auto">
          {edges.map((edge) => {
            const source = summary.nodes[edge.source] ?? unknown(edge.source);
            const target = summary.nodes[edge.target] ?? unknown(edge.target);
            return (
              <li key={edge.id} className="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-1.5 text-sm">
                <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-1.5" title={`${displayName(source)} → ${displayName(target)}`}>
                  <NodeLink node={source} />
                  <span className="text-faint" aria-hidden="true">
                    →
                  </span>
                  <NodeLink node={target} tab={target.label === "DatabaseTable" ? "lineage" : "explore"} />
                </span>
                <span className="flex shrink-0 items-center gap-2 text-xs text-dim">{detail(edge)}</span>
              </li>
            );
          })}
        </ul>
      )}
    </Section>
  );
}

function NoTelemetry({ repositoryId, readOnly }: { repositoryId: string; readOnly: boolean }) {
  return (
    <div className="panel p-5">
      <p className="font-medium">No runtime observations yet</p>
      <p className="mt-1 max-w-3xl text-sm leading-relaxed text-dim">
        {readOnly
          ? "This server is read-only, so it does not accept telemetry. On your own installation you can send traces to the endpoints below."
          : "Send traces from a running system, or press “Simulate traffic” to see what the overlay looks like."}
      </p>
      <dl className="mt-4 grid gap-3 text-sm md:grid-cols-2">
        <div>
          <dt className="eyebrow">OpenTelemetry traces (OTLP/JSON)</dt>
          <dd className="mt-1 break-all font-mono text-[12.5px] text-dim">
            POST /api/v1/repositories/{repositoryId}/telemetry/otlp
          </dd>
        </div>
        <div>
          <dt className="eyebrow">Plain call events</dt>
          <dd className="mt-1 break-all font-mono text-[12.5px] text-dim">
            POST /api/v1/repositories/{repositoryId}/telemetry/events
          </dd>
        </div>
      </dl>
      <p className="mt-4 max-w-3xl text-xs leading-relaxed text-faint">
        Spans are matched to functions by name (<code className="font-mono">code.function.name</code>, or{" "}
        <code className="font-mono">code.namespace</code> plus <code className="font-mono">code.function</code>).
        A span that matches no function in the graph is counted in the response and dropped; it is never
        guessed into the graph.
      </p>
    </div>
  );
}
