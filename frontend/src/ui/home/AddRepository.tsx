"use client";

import { useState, type FormEvent } from "react";

import { api, ApiError } from "@/lib/api";
import { useInterval } from "@/lib/hooks";
import type { Job, SystemInfo } from "@/lib/types";
import { ErrorNote } from "@/ui/common/primitives";

/** Queue an ingestion and follow its progress. */
export function AddRepository({ system, onFinished }: { system: SystemInfo; onFinished: () => void }) {
  const [source, setSource] = useState("");
  const [branch, setBranch] = useState("");
  const [token, setToken] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [job, setJob] = useState<Job | null>(null);

  useInterval(
    () => {
      if (!job) return;
      api.job(job.id).then(
        (next) => {
          setJob(next);
          if (!next.active) onFinished();
        },
        () => undefined, // a missed poll is retried by the next tick
      );
    },
    1000,
    job !== null && job.active,
  );

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!source.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const queued = await api.ingest({
        source: source.trim(),
        branch: branch.trim() || undefined,
        token: token || undefined,
      });
      setJob(queued);
      setToken("");
    } catch (failure) {
      setError(failure instanceof ApiError ? failure : new ApiError(0, "The request failed."));
    } finally {
      setSubmitting(false);
    }
  }

  const hosts = system.allowed_git_hosts;
  return (
    <section className="panel p-4">
      <h2 className="text-lg font-semibold">Analyse a repository</h2>
      <p className="mt-1 text-sm text-dim">
        Give a Git URL the server can clone
        {hosts.length ? ` (allowed hosts: ${hosts.join(", ")})` : ""}
        {system.local_sources_enabled ? ", or a path under an allowed local root" : ""}. History, blame and SQL
        are analysed on the server; nothing is sent to a language model during ingestion.
      </p>
      <form className="mt-3 flex flex-col gap-3" onSubmit={submit}>
        <div className="flex flex-col gap-2 sm:flex-row">
          <label className="sr-only" htmlFor="source">
            Repository URL
          </label>
          <input
            id="source"
            className="input font-mono"
            placeholder="https://github.com/spring-projects/spring-petclinic"
            value={source}
            onChange={(event) => setSource(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            required
          />
          <button type="submit" className="btn btn-primary shrink-0" disabled={submitting || job?.active}>
            {submitting ? "Queueing…" : "Analyse"}
          </button>
        </div>
        <button
          type="button"
          className="self-start text-xs text-dim underline underline-offset-2"
          onClick={() => setAdvanced((open) => !open)}
        >
          {advanced ? "Hide options" : "Branch or private repository"}
        </button>
        {advanced ? (
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="text-xs text-dim">
              Branch (default: the repository default)
              <input
                className="input mt-1 font-mono"
                value={branch}
                onChange={(event) => setBranch(event.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
            </label>
            <label className="text-xs text-dim">
              Access token (used for the clone only, never stored)
              <input
                className="input mt-1 font-mono"
                type="password"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                autoComplete="off"
              />
            </label>
          </div>
        ) : null}
      </form>
      {error ? (
        <div className="mt-3">
          <ErrorNote error={error} />
        </div>
      ) : null}
      {job ? <JobProgress job={job} /> : null}
    </section>
  );
}

function JobProgress({ job }: { job: Job }) {
  const percent = Math.round(Math.min(Math.max(job.progress, 0), 1) * 100);
  const failed = job.status === "failed";
  return (
    <div className="mt-4 rounded-lg border border-line bg-ink p-3 text-sm" aria-live="polite">
      <div className="flex items-center justify-between gap-3">
        <span className="truncate font-mono">{job.name}</span>
        <span className={failed ? "text-danger" : "text-dim"}>
          {failed ? "failed" : job.active ? `${job.stage} · ${percent}%` : "ready"}
        </span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-line">
        <div
          className={`h-full rounded-full transition-[width] duration-300 ${failed ? "bg-danger" : "bg-accent"}`}
          style={{ width: `${failed ? 100 : percent}%` }}
        />
      </div>
      <p className={`mt-2 text-xs ${failed ? "text-danger" : "text-dim"}`}>{failed ? job.error : job.message}</p>
      {job.warnings.length ? (
        <ul className="mt-2 list-disc pl-4 text-xs text-dim">
          {job.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
