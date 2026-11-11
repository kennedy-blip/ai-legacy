"use client";

import Link from "next/link";

import { api } from "@/lib/api";
import { formatNumber, plural } from "@/lib/format";
import { useResource } from "@/lib/hooks";
import { RUN_IT_YOURSELF_URL } from "@/lib/site";
import type { Repository, SystemInfo } from "@/lib/types";
import { Badge, Empty, ErrorNote, Spinner } from "@/ui/common/primitives";

import { AddRepository } from "./AddRepository";

export function Home() {
  const system = useResource("system", (signal) => api.system(signal));
  const repositories = useResource("repositories", (signal) => api.repositories(signal));
  const list = repositories.latest;

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-8 px-4 py-10">
      <section className="max-w-3xl">
        <p className="eyebrow text-accent">Knowledge graph + grounded answers</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight sm:text-4xl">
          Find out what legacy code does, and why it was written that way.
        </h1>
        <p className="mt-3 text-base leading-relaxed text-dim">
          Archaeo-Code parses a repository into a graph of functions, calls, SQL statements, tables, database
          triggers and git history. You can then trace a request down to the rows it changes, see who wrote a
          rule and in which commit, and ask questions whose answers cite the exact lines they rest on.
        </p>
      </section>

      {system.data?.read_only ? <ReadOnlyNotice /> : null}
      {system.error ? <ErrorNote error={system.error} onRetry={system.reload} /> : null}

      <section className="flex flex-col gap-3">
        <div className="flex items-end justify-between gap-3">
          <h2 className="text-lg font-semibold">Repositories</h2>
          {repositories.loading && list ? <Spinner label="Refreshing" /> : null}
        </div>
        {repositories.error ? <ErrorNote error={repositories.error} onRetry={repositories.reload} /> : null}
        {!list && repositories.loading ? (
          <div className="panel">
            <Empty title="Loading repositories">
              <Spinner label="A cold start can take a few seconds" />
            </Empty>
          </div>
        ) : null}
        {list && list.length === 0 ? (
          <div className="panel">
            <Empty title="No repository has been analysed yet">
              {system.data?.read_only
                ? "This instance is read-only and has no graph loaded."
                : "Add one below. The bundled demo is seeded automatically on first start and appears here once it is ready."}
            </Empty>
          </div>
        ) : null}
        {list && list.length > 0 ? (
          <ul className="grid gap-3 sm:grid-cols-2">
            {list.map((repository) => (
              <li key={repository.id}>
                <RepositoryCard repository={repository} />
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      {system.data && !system.data.read_only ? (
        <AddRepository system={system.data} onFinished={repositories.reload} />
      ) : null}

      {system.data ? <Footer system={system.data} /> : null}
    </main>
  );
}

function ReadOnlyNotice() {
  return (
    <div className="rounded-xl border border-accent/40 bg-accent/5 px-4 py-3 text-sm leading-relaxed">
      <p className="font-medium text-accent">This is a read-only demonstration instance.</p>
      <p className="mt-1 text-dim">
        It serves the bundled synthetic repository from a pre-computed graph. The host has no git, no worker
        and no durable disk, so it cannot analyse other repositories or record telemetry. To analyse your own
        code, run the stack yourself:{" "}
        <a
          className="text-fg underline underline-offset-2"
          href={RUN_IT_YOURSELF_URL}
          target="_blank"
          rel="noreferrer"
        >
          instructions
        </a>
        .
      </p>
    </div>
  );
}

function RepositoryCard({ repository }: { repository: Repository }) {
  const ready = repository.status === "ready";
  return (
    <Link
      href={`/r/${encodeURIComponent(repository.id)}`}
      className="panel flex h-full flex-col gap-3 p-4 transition-colors hover:border-line-strong hover:bg-raised"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 truncate font-mono text-[15px] font-medium">{repository.name}</span>
        {repository.is_demo ? (
          <Badge tone="accent" title="Fabricated code and history, shipped with Archaeo-Code">
            synthetic demo
          </Badge>
        ) : null}
        {!ready ? <Badge tone="danger">{repository.status ?? "unknown"}</Badge> : null}
      </div>
      <dl className="grid grid-cols-4 gap-2 text-sm">
        <Count label="files" value={repository.files} />
        <Count label="functions" value={repository.functions} />
        <Count label="tables" value={repository.tables} />
        <Count label="lines" value={repository.lines} />
      </dl>
      <div className="mt-auto flex flex-wrap items-center gap-1.5 text-xs text-faint">
        {repository.languages.slice(0, 5).map((language) => (
          <Badge key={language}>{language}</Badge>
        ))}
        <span className="ml-auto truncate">{repository.source}</span>
      </div>
    </Link>
  );
}

function Count({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dd className="font-semibold tabular-nums">{formatNumber(value)}</dd>
      <dt className="text-xs text-faint">{label}</dt>
    </div>
  );
}

function Footer({ system }: { system: SystemInfo }) {
  return (
    <footer className="mt-auto flex flex-wrap gap-x-4 gap-y-1 border-t border-line pt-4 text-xs text-faint">
      <span>v{system.version}</span>
      <span>graph: {system.graph_backend}</span>
      <span>vectors: {system.vector_backend ?? "off"}</span>
      <span>
        answers:{" "}
        {system.answer_mode === "llm" ? system.llm_model : "graph report (no language model configured)"}
      </span>
      <span>{plural(system.supported_extensions.length, "file type")}</span>
    </footer>
  );
}
