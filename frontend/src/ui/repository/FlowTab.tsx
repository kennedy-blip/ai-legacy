"use client";

import { api } from "@/lib/api";
import { displayName, plural } from "@/lib/format";
import { useResource } from "@/lib/hooks";
import { Centered, Empty, ErrorNote, Spinner } from "@/ui/common/primitives";
import { SequenceDiagram, SequenceLegend } from "@/ui/visualizers/SequenceDiagram";

import { useWorkspace } from "./context";
import { StartPicker } from "./StartPicker";

/** Sequence diagram of one function: its calls in source order, down to tables and triggers. */
export function FlowTab() {
  const { repositoryId, nodeId, open, ask } = useWorkspace();
  const entries = useResource(`entry-points:${repositoryId}`, (signal) => api.entryPoints(repositoryId, signal));
  const sequence = useResource(nodeId ? `sequence:${repositoryId}:${nodeId}` : null, (signal) =>
    api.sequence(repositoryId, nodeId ?? "", signal),
  );
  const options = entries.data?.entry_points ?? [];
  const notFunction = sequence.error?.status === 422;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <StartPicker label="Start at" options={options} selected={nodeId} onPick={(id) => open(id, "flow")} />

      {!nodeId || notFunction ? (
        <div className="panel">
          <Empty title={notFunction ? "A sequence starts at a function" : "Choose where to start"}>
            {notFunction
              ? "The current selection is not a function. Pick an entry point above, or select a function in Explore and open its sequence diagram."
              : "Pick an entry point above, or select any function in Explore. The diagram follows its calls in source order down to the SQL it runs and the triggers the database fires."}
          </Empty>
        </div>
      ) : sequence.error ? (
        <ErrorNote error={sequence.error} onRetry={sequence.reload} />
      ) : !sequence.data ? (
        <Centered>
          <Spinner label="Tracing calls" />
        </Centered>
      ) : (
        <section className="panel flex min-h-0 flex-col">
          <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line px-4 py-2.5">
            <h2 className="font-mono text-sm font-medium">{displayName(sequence.data.root)}</h2>
            <span className="text-xs text-faint">
              {plural(sequence.data.steps.length, "step")} · {plural(sequence.data.participants.length, "participant")}
            </span>
            <div className="ml-auto flex gap-2">
              <button type="button" className="btn" onClick={() => open(sequence.data?.root.id ?? null, "explore")}>
                Open in Explore
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() =>
                  ask(
                    `What happens, step by step, when ${displayName(sequence.data?.root ?? { name: "this", label: "Function" })} runs? Include every table it touches.`,
                    sequence.data?.root.id,
                  )
                }
              >
                Explain this flow
              </button>
            </div>
          </header>
          {sequence.data.steps.length === 0 ? (
            <Empty title="This function calls nothing the analysis could resolve">
              It may only use library code, or its calls go through reflection or dynamic dispatch.
            </Empty>
          ) : (
            <div className="flex flex-col gap-3 p-3">
              <SequenceDiagram sequence={sequence.data} onSelect={(id) => open(id, "explore")} />
              <SequenceLegend />
              <p className="max-w-4xl text-xs leading-relaxed text-faint">
                Derived from source, not from a run: every call the code can make is shown in the order it is
                written, ignoring conditions and loops. Click a step to open what it leads to.
                {sequence.data.participants.length > 8
                  ? ` Scroll sideways to see all ${sequence.data.participants.length} participants.`
                  : ""}
                {sequence.data.truncated ? " The diagram was cut at its step limit." : ""}
              </p>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
