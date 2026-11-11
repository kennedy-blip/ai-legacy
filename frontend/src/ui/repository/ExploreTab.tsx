"use client";

import { useState } from "react";

import { api } from "@/lib/api";
import { useDebounced, useResource } from "@/lib/hooks";
import type { FileEntry } from "@/lib/types";
import { Badge, Centered, Empty, ErrorNote, Spinner } from "@/ui/common/primitives";

import { useWorkspace } from "./context";
import { NodeDetail } from "./NodeDetail";
import { NodeRow } from "./NodeRef";

/** Search on the left, the selected node on the right. */
export function ExploreTab() {
  const { repositoryId, nodeId, open } = useWorkspace();
  const [text, setText] = useState("");
  const [semantic, setSemantic] = useState(true);
  const query = useDebounced(text.trim(), 250);

  const results = useResource(query ? `search:${repositoryId}:${semantic}:${query}` : null, (signal) =>
    api.search(repositoryId, query, { semantic, limit: 40 }, signal),
  );
  const files = useResource(`files:${repositoryId}`, (signal) => api.files(repositoryId, signal));
  const found = results.latest?.results;

  return (
    <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[340px_minmax(0,1fr)]">
      <aside className="panel flex max-h-[calc(100vh-11rem)] min-h-64 flex-col lg:sticky lg:top-16">
        <div className="border-b border-line p-3">
          <label className="sr-only" htmlFor="node-search">
            Search the repository
          </label>
          <input
            id="node-search"
            className="input"
            type="search"
            placeholder="Name, File.java:42, or what it does"
            value={text}
            onChange={(event) => setText(event.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
          <label className="mt-2 flex items-center gap-2 text-xs text-dim">
            <input
              type="checkbox"
              className="accent-accent"
              checked={semantic}
              onChange={(event) => setSemantic(event.target.checked)}
            />
            also match on vocabulary, not only names
          </label>
        </div>
        <div className="min-h-0 flex-1 overflow-auto">
          {query ? (
            results.error ? (
              <div className="p-3">
                <ErrorNote error={results.error} onRetry={results.reload} />
              </div>
            ) : !found ? (
              <Centered>
                <Spinner label="Searching" />
              </Centered>
            ) : found.length === 0 ? (
              <Empty title="Nothing matches">Try a class or table name, or a location such as PaymentService.java:40.</Empty>
            ) : (
              <ul className={`divide-y divide-line ${results.loading ? "opacity-60" : ""}`}>
                {found.map((result) => (
                  <li key={result.id}>
                    <NodeRow
                      node={result}
                      active={result.id === nodeId}
                      trailing={
                        result.match === "name" ? null : (
                          <Badge title={result.match === "semantic" ? "Matched on vocabulary" : "Matched on file and line"}>
                            {result.match === "semantic" ? "similar" : "location"}
                          </Badge>
                        )
                      }
                    />
                  </li>
                ))}
              </ul>
            )
          ) : (
            <FileList files={files.data?.files} loading={files.loading} selected={nodeId} onOpen={(id) => open(id, "explore")} />
          )}
        </div>
      </aside>

      <section className="min-w-0">
        {nodeId ? (
          <NodeDetail key={nodeId} nodeId={nodeId} />
        ) : (
          <div className="panel">
            <Empty title="Pick something to look at">
              Search for a function, class, table or commit, or choose a file. Every panel that opens is built
              from the graph: callers, callees, the SQL it runs, the tables it touches and the commits that
              still own its lines.
            </Empty>
          </div>
        )}
      </section>
    </div>
  );
}

function FileList({
  files,
  loading,
  selected,
  onOpen,
}: {
  files: FileEntry[] | undefined;
  loading: boolean;
  selected: string | null;
  onOpen: (id: string) => void;
}) {
  if (!files) {
    return <Centered>{loading ? <Spinner /> : <span className="text-sm text-dim">No files</span>}</Centered>;
  }
  // Group by directory so long paths stay readable.
  const groups = new Map<string, FileEntry[]>();
  for (const file of files) {
    const cut = file.path.lastIndexOf("/");
    const directory = cut < 0 ? "" : file.path.slice(0, cut);
    groups.set(directory, [...(groups.get(directory) ?? []), file]);
  }
  return (
    <div className="py-1">
      <p className="eyebrow px-3 py-2">Files</p>
      {[...groups.entries()].map(([directory, entries]) => (
        <div key={directory} className="mb-1">
          <p className="truncate px-3 py-1 font-mono text-[11px] text-faint" title={directory}>
            {directory || "."}
          </p>
          <ul>
            {entries.map((file) => (
              <li key={file.id}>
                <button
                  type="button"
                  onClick={() => onOpen(file.id)}
                  className={`flex w-full items-center gap-2 px-3 py-1 pl-5 text-left font-mono text-[13px] transition-colors hover:bg-hover ${
                    file.id === selected ? "bg-raised" : ""
                  }`}
                >
                  <span className="min-w-0 flex-1 truncate">{file.path.slice(file.path.lastIndexOf("/") + 1)}</span>
                  {file.parse_error ? <Badge tone="danger">parse error</Badge> : null}
                  <span className="shrink-0 text-[11px] tabular-nums text-faint">{file.lines}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
