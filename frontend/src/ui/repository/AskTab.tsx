"use client";

import {
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type Ref,
} from "react";

import { api, explain, type ExplainRequest } from "@/lib/api";
import { plural } from "@/lib/format";
import { useResource } from "@/lib/hooks";
import type { Citation, CitationSummary, Evidence } from "@/lib/types";
import { Badge, ErrorNote, Spinner } from "@/ui/common/primitives";
import { Markdown } from "@/ui/common/Markdown";

import { useWorkspace } from "./context";
import { SourceViewer } from "./SourceViewer";

export interface AskHandle {
  /** Ask a question as if the user had typed it, optionally about one node. */
  ask: (question: string, nodeId: string | null) => void;
}

interface Exchange {
  id: number;
  question: string;
  nodeId: string | null;
  status: "running" | "done" | "error" | "stopped";
  answer: string;
  mode: "llm" | "graph-report" | null;
  model: string | null;
  evidence: Evidence[];
  notes: string[];
  citations: Citation[] | null;
  summary: CitationSummary | null;
  warnings: string[];
  error: string | null;
}

interface Preview {
  path: string;
  start: number;
  end: number;
  nodeId: string | null;
  /** Evidence text to show when the server no longer has the file. */
  fallback: string | null;
}

const LOOKS_LIKE_CITATION = /^(?:commit\s+[0-9a-f]{7,40}|[\w./\\$@-]+\.[A-Za-z0-9]{1,8}:\d+(?:\s*-\s*\d+)?)$/i;
const CONTEXT_LINES = 12;

function blank(id: number, question: string, nodeId: string | null): Exchange {
  return {
    id,
    question,
    nodeId,
    status: "running",
    answer: "",
    mode: null,
    model: null,
    evidence: [],
    notes: [],
    citations: null,
    summary: null,
    warnings: [],
    error: null,
  };
}

/** Questions answered from the graph, with every citation checked against the repository. */
export function AskTab({ ref }: { ref?: Ref<AskHandle> }) {
  const { repositoryId, system, open } = useWorkspace();
  const suggestions = useResource(`suggestions:${repositoryId}`, (signal) => api.suggestions(repositoryId, signal));
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [draft, setDraft] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const controller = useRef<AbortController | null>(null);
  const counter = useRef(0);
  const latest = useRef<Exchange[]>([]);
  const previewBox = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    latest.current = exchanges;
  });
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (preview) previewBox.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [preview]);

  const patch = useCallback((id: number, change: (exchange: Exchange) => Exchange) => {
    setExchanges((current) => current.map((exchange) => (exchange.id === id ? change(exchange) : exchange)));
  }, []);

  const stream = useCallback(
    async (id: number, body: ExplainRequest, signal: AbortSignal) => {
      // Tokens arrive faster than the screen refreshes; paint them once per frame.
      let buffered = "";
      let frame = 0;
      const flush = () => {
        if (frame) window.cancelAnimationFrame(frame);
        frame = 0;
        if (!buffered) return;
        const text = buffered;
        buffered = "";
        patch(id, (exchange) => ({ ...exchange, answer: exchange.answer + text }));
      };
      let finished = false;
      try {
        for await (const event of explain(repositoryId, body, signal)) {
          if (event.type === "token") {
            buffered += event.text;
            if (!frame) frame = window.requestAnimationFrame(flush);
            continue;
          }
          flush();
          if (event.type === "context") {
            patch(id, (exchange) => ({
              ...exchange,
              mode: event.mode,
              model: event.model,
              evidence: event.evidence,
              notes: event.notes,
            }));
          } else if (event.type === "warning") {
            patch(id, (exchange) => ({ ...exchange, warnings: [...exchange.warnings, event.message] }));
          } else if (event.type === "citations") {
            patch(id, (exchange) => ({ ...exchange, citations: event.citations, summary: event.summary }));
          } else if (event.type === "done") {
            finished = true;
            patch(id, (exchange) => ({
              ...exchange,
              status: "done",
              answer: event.answer || exchange.answer,
              warnings: event.warnings.length ? event.warnings : exchange.warnings,
            }));
          } else if (event.type === "error") {
            finished = true;
            patch(id, (exchange) => ({ ...exchange, status: "error", error: event.message }));
          }
        }
        flush();
        if (!finished) {
          patch(id, (exchange) => ({
            ...exchange,
            status: "error",
            error: "The answer stopped before it was complete. The server may have run out of time; ask again.",
          }));
        }
      } catch (error) {
        flush();
        if (signal.aborted) {
          patch(id, (exchange) => (exchange.status === "running" ? { ...exchange, status: "stopped" } : exchange));
        } else {
          const message = error instanceof Error ? error.message : "The request failed.";
          patch(id, (exchange) => ({ ...exchange, status: "error", error: message }));
        }
      }
    },
    [repositoryId, patch],
  );

  const submit = useCallback(
    (question: string, nodeId: string | null) => {
      const text = question.trim();
      if (!text) return;
      controller.current?.abort();
      const abort = new AbortController();
      controller.current = abort;
      counter.current += 1;
      const id = counter.current;
      // Earlier finished exchanges give a follow-up question its context.
      const history = latest.current
        .filter((exchange) => exchange.status === "done" && exchange.answer)
        .slice(-3)
        .flatMap((exchange) => [
          { role: "user" as const, content: exchange.question },
          { role: "assistant" as const, content: exchange.answer.slice(0, 6000) },
        ]);
      setExchanges((current) => [
        ...current.map((exchange) =>
          exchange.status === "running" ? { ...exchange, status: "stopped" as const } : exchange,
        ),
        blank(id, text.slice(0, 4000), nodeId),
      ]);
      setSelected(null);
      setPreview(null);
      setDraft("");
      void stream(id, { question: text.slice(0, 4000), focus_node_id: nodeId, history }, abort.signal);
    },
    [stream],
  );

  useImperativeHandle(ref, () => ({ ask: submit }), [submit]);

  // A link can carry a question: ?tab=ask&q=… (optionally &node=…). It is asked once and
  // then taken out of the address, so reloading or going back does not ask it again.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const query = new URLSearchParams(window.location.search);
      const question = query.get("q")?.trim();
      if (!question) return;
      query.delete("q");
      query.set("tab", "ask");
      window.history.replaceState(null, "", `?${query.toString()}`);
      submit(question, query.get("node"));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [submit]);

  const running = exchanges.some((exchange) => exchange.status === "running");
  const newestFirst = useMemo(() => [...exchanges].reverse(), [exchanges]);
  const shown = exchanges.find((exchange) => exchange.id === selected) ?? exchanges[exchanges.length - 1] ?? null;
  const graphReport = system?.answer_mode === "graph-report";

  const openCitation = useCallback(
    (citation: Citation) => {
      if (citation.kind === "commit" || !citation.path || !citation.line) {
        if (citation.node_id) open(citation.node_id, "explore");
        return;
      }
      setPreview({
        path: citation.path,
        start: citation.line,
        end: citation.end_line ?? citation.line,
        nodeId: citation.node_id,
        fallback: null,
      });
    },
    [open],
  );

  const openEvidence = (evidence: Evidence) => {
    if (!evidence.path || !evidence.start_line) {
      open(evidence.node_id, "explore");
      return;
    }
    setPreview({
      path: evidence.path,
      start: evidence.start_line,
      end: evidence.end_line ?? evidence.start_line,
      nodeId: evidence.node_id,
      fallback: evidence.text || null,
    });
  };

  return (
    <div className="grid min-h-0 flex-1 items-start gap-4 xl:grid-cols-[minmax(0,1fr)_440px]">
      <div className="flex min-w-0 flex-col gap-4">
        <form
          className="panel p-3"
          onSubmit={(event) => {
            event.preventDefault();
            submit(draft, null);
          }}
        >
          <label className="sr-only" htmlFor="question">
            Question about this repository
          </label>
          <textarea
            id="question"
            className="input min-h-20 resize-y"
            placeholder="Ask why something exists, what a function does, who writes a table…"
            value={draft}
            maxLength={4000}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                submit(draft, null);
              }
            }}
          />
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <p className="min-w-0 flex-1 text-xs leading-relaxed text-faint">
              {graphReport
                ? "No language model is configured on this server. Answers are reports assembled from graph facts; every line links to its source."
                : system?.llm_model
                  ? `Written by ${system.llm_model} from evidence retrieved from the graph. Every citation is checked against the repository afterwards.`
                  : "Answers are built from evidence retrieved from the graph; every citation is checked against the repository."}
            </p>
            {running ? (
              <button type="button" className="btn" onClick={() => controller.current?.abort()}>
                Stop
              </button>
            ) : null}
            <button type="submit" className="btn btn-primary" disabled={!draft.trim()}>
              Ask
            </button>
          </div>
        </form>

        {exchanges.length === 0 ? (
          <div className="panel p-4">
            <p className="eyebrow">Try one of these</p>
            {suggestions.data ? (
              <ul className="mt-2 flex flex-col gap-1.5">
                {suggestions.data.suggestions.map((suggestion) => (
                  <li key={suggestion.question}>
                    <button
                      type="button"
                      className="w-full rounded-lg border border-line px-3 py-2 text-left text-sm text-dim transition-colors hover:border-line-strong hover:bg-hover hover:text-fg"
                      onClick={() => submit(suggestion.question, suggestion.node_id)}
                    >
                      {suggestion.question}
                    </button>
                  </li>
                ))}
              </ul>
            ) : suggestions.error ? (
              <p className="mt-2 text-sm text-dim">Suggestions are unavailable; type a question above.</p>
            ) : (
              <div className="mt-3">
                <Spinner />
              </div>
            )}
          </div>
        ) : null}

        {newestFirst.map((exchange) => (
          <Answer
            key={exchange.id}
            exchange={exchange}
            active={shown?.id === exchange.id}
            onSelect={() => setSelected(exchange.id)}
            onCitation={openCitation}
          />
        ))}
      </div>

      <aside className="flex min-w-0 flex-col gap-4 xl:sticky xl:top-16 xl:max-h-[calc(100vh-5rem)] xl:overflow-auto">
        {preview ? (
          <div ref={previewBox} className="flex flex-col gap-2">
            <PreviewPane
              key={`${preview.path}:${preview.start}`}
              repositoryId={repositoryId}
              preview={preview}
              onClose={() => setPreview(null)}
              onOpen={preview.nodeId ? () => open(preview.nodeId, "explore") : null}
            />
          </div>
        ) : null}
        {shown ? <EvidenceList exchange={shown} onOpen={openEvidence} /> : <HowItWorks graphReport={graphReport} />}
      </aside>
    </div>
  );
}

function Answer({
  exchange,
  active,
  onSelect,
  onCitation,
}: {
  exchange: Exchange;
  active: boolean;
  onSelect: () => void;
  onCitation: (citation: Citation) => void;
}) {
  const lookup = useMemo(() => {
    const map = new Map<string, Citation>();
    for (const citation of exchange.citations ?? []) map.set(citation.raw.trim(), citation);
    return map;
  }, [exchange.citations]);
  const checked = exchange.citations !== null;

  const reference = useCallback(
    (raw: string, key: string): ReactNode | null => {
      const citation = lookup.get(raw.trim());
      if (citation) return <CitationChip key={key} citation={citation} onOpen={onCitation} />;
      if (!checked && LOOKS_LIKE_CITATION.test(raw.trim())) {
        return (
          <span
            key={key}
            title="Not checked yet"
            className="mx-0.5 inline-block rounded border border-line-strong px-1 font-mono text-[0.82em] text-faint"
          >
            {raw}
          </span>
        );
      }
      return null;
    },
    [lookup, checked, onCitation],
  );

  const summary = exchange.summary;
  return (
    <article className={`panel ${active ? "border-line-strong" : ""}`}>
      <header className="flex flex-wrap items-start gap-2 border-b border-line px-4 py-3">
        <h2 className="min-w-0 flex-1 text-[15px] font-medium leading-snug">{exchange.question}</h2>
        {exchange.mode === "graph-report" ? (
          <Badge title="No language model is configured: this answer is assembled from graph facts, not written by a model">
            graph report
          </Badge>
        ) : exchange.model ? (
          <Badge title="Written by a language model from retrieved graph evidence">{exchange.model}</Badge>
        ) : null}
        {exchange.nodeId ? <Badge title="The question was asked about one selected node">focused</Badge> : null}
      </header>

      <div className="px-4 py-3">
        {exchange.notes.length ? (
          <ul className="mb-3 list-disc pl-5 text-xs leading-relaxed text-faint">
            {exchange.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        ) : null}
        {exchange.answer ? <Markdown text={exchange.answer} reference={reference} /> : null}
        {exchange.status === "running" ? (
          <div className={exchange.answer ? "mt-3" : ""}>
            <Spinner label={exchange.evidence.length ? "Writing" : "Collecting evidence from the graph"} />
          </div>
        ) : null}
        {exchange.status === "stopped" ? <p className="mt-3 text-sm text-faint">Stopped.</p> : null}
        {exchange.status === "error" && exchange.error ? (
          <div className="mt-3">
            <ErrorNote error={new Error(exchange.error)} />
          </div>
        ) : null}
      </div>

      {summary || exchange.warnings.length || exchange.evidence.length ? (
        <footer className="flex flex-col gap-2 border-t border-line px-4 py-2.5 text-xs">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            {summary ? <Verdict summary={summary} /> : null}
            {exchange.evidence.length ? (
              <button type="button" className="ml-auto text-dim underline underline-offset-2 hover:text-fg" onClick={onSelect}>
                {plural(exchange.evidence.length, "piece")} of evidence
              </button>
            ) : null}
          </div>
          {exchange.warnings.map((warning) => (
            <p key={warning} className="text-runtime">
              {warning}
            </p>
          ))}
        </footer>
      ) : null}
    </article>
  );
}

function Verdict({ summary }: { summary: CitationSummary }) {
  if (summary.total === 0) {
    return <span className="text-runtime">This answer cites nothing, so none of it can be checked.</span>;
  }
  const unverified = summary.outside_context + summary.unresolved;
  return (
    <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <span className={unverified === 0 ? "text-tbl" : "text-dim"}>
        {summary.verified} of {plural(summary.total, "citation")} verified
      </span>
      {summary.outside_context ? (
        <span className="text-runtime" title="The location exists, but it was not part of the evidence the answer was built from">
          {summary.outside_context} outside the evidence
        </span>
      ) : null}
      {summary.unresolved ? (
        <span className="text-danger" title="No such file, line or commit exists in this repository">
          {summary.unresolved} not found
        </span>
      ) : null}
    </span>
  );
}

const CHIP: Record<Citation["status"], { className: string; title: string }> = {
  verified: {
    className: "border-tbl/40 text-tbl hover:bg-tbl/10",
    title: "Verified: this location is part of the evidence the answer was built from",
  },
  "outside-context": {
    className: "border-runtime/50 text-runtime hover:bg-runtime/10",
    title: "Exists in the repository, but was not part of the evidence, so the claim next to it is not grounded",
  },
  unresolved: {
    className: "border-danger/50 text-danger line-through decoration-danger/60",
    title: "No such file, line or commit exists in this repository",
  },
};

function CitationChip({ citation, onOpen }: { citation: Citation; onOpen: (citation: Citation) => void }) {
  const style = CHIP[citation.status] ?? CHIP.unresolved;
  const reachable = Boolean(citation.node_id || (citation.path && citation.line));
  return (
    <button
      type="button"
      disabled={!reachable}
      onClick={() => onOpen(citation)}
      title={citation.detail ? `${style.title}. ${citation.detail}` : style.title}
      className={`mx-0.5 inline-block rounded border px-1 font-mono text-[0.82em] transition-colors disabled:cursor-default ${style.className}`}
    >
      {citation.raw}
    </button>
  );
}

function PreviewPane({
  repositoryId,
  preview,
  onClose,
  onOpen,
}: {
  repositoryId: string;
  preview: Preview;
  onClose: () => void;
  onOpen: (() => void) | null;
}) {
  const source = useResource(`preview:${repositoryId}:${preview.path}:${preview.start}:${preview.end}`, (signal) =>
    api.fileSource(
      repositoryId,
      preview.path,
      { start: Math.max(preview.start - CONTEXT_LINES, 1), end: preview.end + CONTEXT_LINES },
      signal,
    ),
  );
  return (
    <>
      <div className="flex items-center gap-2">
        <p className="eyebrow min-w-0 flex-1">Source</p>
        {onOpen ? (
          <button type="button" className="btn" onClick={onOpen}>
            Open in Explore
          </button>
        ) : null}
        <button type="button" className="btn btn-ghost" onClick={onClose} aria-label="Close the source preview">
          Close
        </button>
      </div>
      {source.data ? (
        <SourceViewer slice={source.data} highlight={{ start: preview.start, end: preview.end }} maxHeight="24rem" />
      ) : source.error ? (
        preview.fallback ? (
          <pre className="max-h-96 overflow-auto rounded-lg border border-line bg-ink p-3 font-mono text-[12.5px] leading-relaxed text-fg">
            {preview.fallback}
          </pre>
        ) : (
          <p className="text-sm text-faint">
            {source.error.status === 404 || source.error.status === 409
              ? "The source text of this file is not available on this server."
              : source.error.message}
          </p>
        )
      ) : (
        <Spinner label="Loading source" />
      )}
    </>
  );
}

const ROLE_NOTE: Record<string, string> = {
  focus: "what the question is about",
  caller: "calls it",
  callee: "called by it",
  data: "data it touches",
  sql: "SQL it runs",
  "side-effect": "what the database does on its own",
  history: "why it changed",
};

function EvidenceList({ exchange, onOpen }: { exchange: Exchange; onOpen: (evidence: Evidence) => void }) {
  if (exchange.evidence.length === 0) {
    return (
      <div className="panel p-4 text-sm text-dim">
        {exchange.status === "running" ? <Spinner label="Collecting evidence" /> : "No evidence was retrieved for this question."}
      </div>
    );
  }
  return (
    <section className="panel flex min-w-0 flex-col">
      <header className="border-b border-line px-4 py-2.5">
        <h2 className="text-sm font-semibold">Evidence</h2>
        <p className="mt-0.5 text-xs leading-relaxed text-faint">
          What was retrieved from the graph for “{exchange.question.length > 70 ? `${exchange.question.slice(0, 69)}…` : exchange.question}”.
          The answer may only cite what is listed here.
        </p>
      </header>
      <ul className="divide-y divide-line">
        {exchange.evidence.map((evidence) => (
          <li key={evidence.id}>
            <button
              type="button"
              onClick={() => onOpen(evidence)}
              className="flex w-full min-w-0 flex-col gap-0.5 px-4 py-2 text-left transition-colors hover:bg-hover"
            >
              <span className="flex min-w-0 items-center gap-2">
                <span className="shrink-0 font-mono text-[11px] text-faint">{evidence.id}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-[13px]">{evidence.title}</span>
                <Badge tone={evidence.kind === "commit" ? "hist" : evidence.kind === "sql" ? "qry" : "fn"}>
                  {evidence.kind}
                </Badge>
              </span>
              <span className="truncate text-xs text-faint">
                {evidence.citation ? `${evidence.citation} · ` : ""}
                {evidence.reason || ROLE_NOTE[evidence.role] || evidence.role}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function HowItWorks({ graphReport }: { graphReport: boolean }) {
  return (
    <section className="panel p-4 text-sm leading-relaxed text-dim">
      <h2 className="text-sm font-semibold text-fg">How answers are built</h2>
      <ol className="mt-2 flex list-decimal flex-col gap-1.5 pl-5">
        <li>The question is matched to functions, classes, tables and commits by name and by vocabulary.</li>
        <li>
          The graph is walked from those: callers, callees, the SQL they run, the tables and triggers behind
          it, and the commits that still own the lines.
        </li>
        <li>
          {graphReport
            ? "Those facts are laid out as a report. No model writes anything on this server."
            : "A language model writes the answer from that evidence only."}
        </li>
        <li>
          Every <span className="font-mono text-[0.9em]">[File:line]</span> and{" "}
          <span className="font-mono text-[0.9em]">[commit hash]</span> in the answer is then checked against the
          repository and coloured: <span className="text-tbl">verified</span>,{" "}
          <span className="text-runtime">outside the evidence</span> or <span className="text-danger">not found</span>.
        </li>
      </ol>
    </section>
  );
}
