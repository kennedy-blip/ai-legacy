"use client";

import { useMemo } from "react";

import { api } from "@/lib/api";
import { displayName, plural } from "@/lib/format";
import { useResource } from "@/lib/hooks";
import type { NodeBrief } from "@/lib/types";
import { Centered, Empty, ErrorNote, Spinner } from "@/ui/common/primitives";
import { GraphLegend } from "@/ui/visualizers/LayeredGraph";
import { LineageView } from "@/ui/visualizers/LineageView";

import { useWorkspace } from "./context";
import { StartPicker } from "./StartPicker";

/** Data lineage: for a table, everything that reaches it; for a function, every table it reaches. */
export function LineageTab() {
  const { repositoryId, repository, nodeId, open, ask } = useWorkspace();
  const entries = useResource(`entry-points:${repositoryId}`, (signal) => api.entryPoints(repositoryId, signal));
  const lineage = useResource(nodeId ? `lineage:${repositoryId}:${nodeId}` : null, (signal) =>
    api.lineage(repositoryId, nodeId ?? "", signal),
  );

  const tables = useMemo<NodeBrief[]>(
    () =>
      (repository.insights.hot_tables ?? []).map((table) => ({
        id: table.id,
        label: "DatabaseTable",
        name: table.name,
        location: null,
      })),
    [repository],
  );
  const unsupported = lineage.error?.status === 422;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <StartPicker label="Table" options={tables} selected={nodeId} onPick={(id) => open(id, "lineage")} />
      <StartPicker
        label="Entry point"
        options={entries.data?.entry_points ?? []}
        selected={nodeId}
        onPick={(id) => open(id, "lineage")}
      />

      {!nodeId || unsupported ? (
        <div className="panel">
          <Empty title={unsupported ? "Lineage starts at a table, a function or an SQL statement" : "Choose a table or an entry point"}>
            For a table you get every entry point, function and SQL statement that reads or writes it, plus
            the triggers its writes fire. For a function you get every table it reaches, however deep.
          </Empty>
        </div>
      ) : lineage.error ? (
        <ErrorNote error={lineage.error} onRetry={lineage.reload} />
      ) : !lineage.data ? (
        <Centered>
          <Spinner label="Following the data" />
        </Centered>
      ) : (
        <section className="panel flex min-h-0 flex-col">
          <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line px-4 py-2.5">
            <h2 className="font-mono text-sm font-medium">{displayName(lineage.data.root)}</h2>
            <span className="text-xs text-faint">{plural(lineage.data.nodes.length, "node")}</span>
            <div className="ml-auto flex gap-2">
              <button type="button" className="btn" onClick={() => open(lineage.data?.root.id ?? null, "explore")}>
                Open in Explore
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => {
                  const root = lineage.data?.root;
                  if (!root) return;
                  ask(
                    root.label === "DatabaseTable"
                      ? `Which code writes to ${root.name}, and are there triggers or other hidden side effects?`
                      : `Which tables does ${displayName(root)} read and write, directly or through the code it calls?`,
                    root.id,
                  );
                }}
              >
                Explain this lineage
              </button>
            </div>
          </header>
          {lineage.data.nodes.length <= 1 ? (
            <Empty title="No data access found">Nothing in the graph connects this node to a table.</Empty>
          ) : (
            <div className="flex flex-col gap-3 p-3">
              <LineageView lineage={lineage.data} selectedId={nodeId} onSelect={(id) => open(id, "explore")} />
              <GraphLegend lineage />
              <p className="max-w-4xl text-xs leading-relaxed text-faint">
                “Hidden side effects” are things the database does on its own when a table is written: triggers,
                the routines they call and the tables those change. They do not appear in the application code.
                {lineage.data.truncated ? " The graph was cut at its node limit." : ""}
              </p>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
