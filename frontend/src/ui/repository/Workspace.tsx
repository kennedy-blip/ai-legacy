"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef } from "react";

import { api } from "@/lib/api";
import { formatDate, formatNumber } from "@/lib/format";
import { useResource } from "@/lib/hooks";
import type { RepositoryDetail } from "@/lib/types";
import { Badge, Centered, Empty, ErrorNote, Spinner } from "@/ui/common/primitives";

import { AskTab, type AskHandle } from "./AskTab";
import { isTab, TABS, WorkspaceProvider, type TabId, type WorkspaceValue } from "./context";
import { ExploreTab } from "./ExploreTab";
import { FlowTab } from "./FlowTab";
import { LineageTab } from "./LineageTab";
import { MapTab } from "./MapTab";
import { Overview } from "./Overview";
import { RuntimeTab } from "./RuntimeTab";

export function Workspace() {
  const params = useParams<{ repo: string }>();
  const repositoryId = decodeURIComponent(params.repo);
  const search = useSearchParams();

  const requested = search.get("tab");
  const tab: TabId = isTab(requested) ? requested : "overview";
  const nodeId = search.get("node");

  const repository = useResource(`repository:${repositoryId}`, (signal) => api.repository(repositoryId, signal));
  const system = useResource("system", (signal) => api.system(signal));
  const [askTab, askQuestion] = useAskTab();

  // Tab and selection live in the query string. The History API updates it without a
  // server round trip; Next keeps useSearchParams in step.
  const navigate = useCallback((next: { tab?: TabId; node?: string | null }) => {
    const query = new URLSearchParams(window.location.search);
    if (next.tab !== undefined) query.set("tab", next.tab);
    if (next.node !== undefined) {
      if (next.node === null) query.delete("node");
      else query.set("node", next.node);
    }
    const text = query.toString();
    if (text === window.location.search.replace(/^\?/, "")) return;
    window.history.pushState(null, "", text ? `?${text}` : window.location.pathname);
    window.scrollTo({ top: 0 });
  }, []);

  const open = useCallback(
    (id: string | null, target?: TabId) => navigate({ node: id, tab: target }),
    [navigate],
  );
  const ask = useCallback(
    (question: string, about?: string | null) => {
      askQuestion(question, about ?? null);
      navigate({ tab: "ask", node: about === undefined ? undefined : about });
    },
    [navigate, askQuestion],
  );

  const detail = repository.latest;
  const reload = repository.reload;

  // On a narrow screen the tab strip scrolls sideways; keep the current tab in sight.
  // Only the strip is moved, so the page's own scroll position is left alone.
  const strip = useRef<HTMLDivElement>(null);
  const ready = Boolean(detail);
  useEffect(() => {
    const container = strip.current;
    const current = container?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!container || !current) return;
    const box = container.getBoundingClientRect();
    const item = current.getBoundingClientRect();
    if (item.left < box.left) container.scrollLeft -= box.left - item.left + 16;
    else if (item.right > box.right) container.scrollLeft += item.right - box.right + 16;
  }, [tab, ready]);
  const value = useMemo<WorkspaceValue | null>(
    () =>
      detail
        ? { repositoryId, repository: detail, system: system.data, nodeId, open, ask, refresh: reload }
        : null,
    [repositoryId, detail, system.data, nodeId, open, ask, reload],
  );

  if (repository.error && !detail) {
    return (
      <Centered>
        <div className="flex max-w-md flex-col items-center gap-3">
          {repository.error.status === 404 ? (
            <Empty title="This repository is not in the graph">
              It may have been removed, or the link belongs to another installation.
            </Empty>
          ) : (
            <ErrorNote error={repository.error} onRetry={repository.reload} />
          )}
          <Link href="/" className="btn">
            All repositories
          </Link>
        </div>
      </Centered>
    );
  }
  if (!detail || !value) {
    return (
      <Centered>
        <Spinner label="Opening repository" />
      </Centered>
    );
  }

  return (
    <WorkspaceProvider value={value}>
      <div className="flex min-h-0 flex-1 flex-col">
        <RepositoryBar repository={detail} />
        <nav className="border-b border-line bg-ink" aria-label="Views">
          <div ref={strip} className="mx-auto flex w-full max-w-[1600px] gap-1 overflow-x-auto px-3">
            {TABS.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => navigate({ tab: item.id })}
                aria-current={item.id === tab ? "page" : undefined}
                className={`-mb-px shrink-0 border-b-2 px-3 py-2.5 text-sm transition-colors ${
                  item.id === tab
                    ? "border-accent font-medium text-fg"
                    : "border-transparent text-dim hover:text-fg"
                }`}
              >
                {item.label}
              </button>
            ))}
          </div>
        </nav>
        <main className="mx-auto flex min-h-0 w-full max-w-[1600px] flex-1 flex-col px-4 py-4">
          {tab === "overview" ? <Overview /> : null}
          {tab === "explore" ? <ExploreTab /> : null}
          {tab === "flow" ? <FlowTab /> : null}
          {tab === "lineage" ? <LineageTab /> : null}
          {tab === "map" ? <MapTab /> : null}
          {tab === "runtime" ? <RuntimeTab /> : null}
          <div hidden={tab !== "ask"} className={tab === "ask" ? "flex min-h-0 flex-1 flex-col" : undefined}>
            <AskTab ref={askTab} />
          </div>
        </main>
      </div>
    </WorkspaceProvider>
  );
}

/**
 * The Ask tab stays mounted (hidden) so a conversation survives switching views;
 * other views hand it questions through this handle.
 */
function useAskTab() {
  const handle = useRef<AskHandle>(null);
  const ask = useCallback((question: string, nodeId: string | null) => handle.current?.ask(question, nodeId), []);
  return [handle, ask] as const;
}

function RepositoryBar({ repository }: { repository: RepositoryDetail }) {
  const history = repository.stats.history;
  return (
    <div className="border-b border-line bg-panel">
      <div className="mx-auto flex w-full max-w-[1600px] flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3">
        <h1 className="font-mono text-lg font-medium tracking-tight">{repository.name}</h1>
        {repository.is_demo ? (
          <Badge
            tone="accent"
            title="The code, the people, the tickets and the history of this repository are fabricated for demonstration"
          >
            synthetic demo
          </Badge>
        ) : null}
        {repository.status !== "ready" ? <Badge tone="danger">{repository.status}</Badge> : null}
        <dl className="ml-auto flex flex-wrap gap-x-4 gap-y-1 text-xs text-dim">
          <Meta label="branch" value={repository.branch ?? "–"} />
          <Meta label="commit" value={repository.head_commit?.slice(0, 8) ?? "–"} />
          <Meta label="functions" value={formatNumber(repository.functions)} />
          <Meta label="tables" value={formatNumber(repository.tables)} />
          {history?.first_commit_at ? (
            <Meta
              label="history"
              value={`${formatDate(history.first_commit_at)} → ${formatDate(history.last_commit_at)}`}
            />
          ) : null}
          <Meta
            label={repository.from_snapshot ? "graph" : "analysed"}
            value={repository.from_snapshot ? "pre-computed snapshot" : formatDate(repository.ingested_at)}
          />
        </dl>
      </div>
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-1.5">
      <dt className="text-faint">{label}</dt>
      <dd className="font-mono text-fg">{value}</dd>
    </div>
  );
}
