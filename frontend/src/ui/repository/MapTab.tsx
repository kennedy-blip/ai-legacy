"use client";

import { api } from "@/lib/api";
import { plural } from "@/lib/format";
import { useResource } from "@/lib/hooks";
import { Centered, Empty, ErrorNote, Spinner } from "@/ui/common/primitives";
import { TopologyLegend, TopologyMap } from "@/ui/visualizers/TopologyMap";

import { useWorkspace } from "./context";

/** Architecture map: how groups of code depend on each other and on the database. */
export function MapTab() {
  const { repositoryId, open } = useWorkspace();
  const topology = useResource(`topology:${repositoryId}`, (signal) => api.topology(repositoryId, signal));

  if (topology.error) return <ErrorNote error={topology.error} onRetry={topology.reload} />;
  if (!topology.data) {
    return (
      <Centered>
        <Spinner label="Grouping the code" />
      </Centered>
    );
  }
  const { groups, links } = topology.data;
  if (groups.length === 0) {
    return (
      <div className="panel">
        <Empty title="Nothing to map">No code was parsed in this repository.</Empty>
      </div>
    );
  }
  const tables = groups.filter((group) => group.kind === "table").length;
  return (
    <section className="panel flex min-h-0 flex-col">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-line px-4 py-2.5">
        <h2 className="text-sm font-semibold">Topology</h2>
        <span className="text-xs text-faint">
          {plural(groups.length - tables, "code group")} · {plural(tables, "table")} · {plural(links.length, "dependency", "dependencies")}
        </span>
      </header>
      <div className="flex flex-col gap-3 p-3">
        <TopologyMap topology={topology.data} onSelectTable={(id) => open(id, "lineage")} />
        <TopologyLegend />
        <p className="max-w-4xl text-xs leading-relaxed text-faint">
          Each box on the left is a directory; a link is the sum of the calls, reads or writes between two
          groups. Hover a box to isolate its dependencies; click a table to follow its data lineage.
        </p>
      </div>
    </section>
  );
}
