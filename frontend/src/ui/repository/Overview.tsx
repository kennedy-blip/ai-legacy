"use client";

import { api } from "@/lib/api";
import { formatDate, formatNumber, plural, TONE_HEX } from "@/lib/format";
import { useResource } from "@/lib/hooks";
import type { InsightItem, NodeBrief } from "@/lib/types";
import { Badge, Empty, Section, Stat } from "@/ui/common/primitives";

import { useWorkspace } from "./context";
import { NodeRow } from "./NodeRef";

const LANGUAGE_COLOURS = [TONE_HEX.fn, TONE_HEX.tbl, TONE_HEX.hist, TONE_HEX.cls, TONE_HEX.qry, TONE_HEX.runtime];

export function Overview() {
  const { repository, repositoryId, open, ask } = useWorkspace();
  const { stats, insights, graph } = repository;
  const entries = useResource(`entry-points:${repositoryId}`, (signal) => api.entryPoints(repositoryId, signal));
  const suggestions = useResource(`suggestions:${repositoryId}`, (signal) => api.suggestions(repositoryId, signal));

  const lines = Object.entries(stats.lines_by_language ?? {}).sort((a, b) => b[1] - a[1]);
  const totalLines = lines.reduce((sum, [, count]) => sum + count, 0) || 1;
  const dead = insights.dead_candidates;

  return (
    <div className="flex flex-col gap-4">
      {repository.is_demo ? (
        <p className="rounded-lg border border-line bg-panel px-3 py-2 text-sm text-dim">
          <span className="font-medium text-accent">Synthetic demo.</span> A small billing system written for
          this product, with twelve years of fabricated history by four fictional authors. It contains the
          things Archaeo-Code is built to surface: a rule explained only in a commit message, a hidden database
          trigger, dead code.
        </p>
      ) : null}
      {repository.warnings.length ? (
        <ul className="rounded-lg border border-runtime/40 bg-runtime/5 px-3 py-2 text-sm text-runtime">
          {repository.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-8">
        <Stat label="Files" value={formatNumber(repository.files)} />
        <Stat label="Lines" value={formatNumber(repository.lines)} />
        <Stat label="Classes" value={formatNumber(repository.classes)} />
        <Stat label="Functions" value={formatNumber(repository.functions)} />
        <Stat label="Calls" value={formatNumber(stats.calls)} hint="Resolved call relationships" />
        <Stat label="SQL" value={formatNumber(stats.queries)} hint="SQL statements found in code and .sql files" />
        <Stat label="Tables" value={formatNumber(repository.tables)} />
        <Stat
          label="Commits"
          value={formatNumber(stats.history?.commits)}
          hint={`${formatNumber(stats.history?.developers)} authors`}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Section title="Where execution starts" aside={plural(insights.entry_point_count ?? 0, "entry point")}>
          {entries.data && entries.data.entry_points.length > 0 ? (
            <ul className="max-h-72 divide-y divide-line overflow-auto">
              {entries.data.entry_points.map((node) => (
                <li key={node.id}>
                  <NodeRow node={node} tab="flow" />
                </li>
              ))}
            </ul>
          ) : (
            <Empty title={entries.loading ? "Loading" : "No entry point detected"}>
              {entries.loading ? null : "HTTP handlers, main functions and scheduled jobs are listed here."}
            </Empty>
          )}
        </Section>

        <Section title="Tables under the most code" aside="readers · writers">
          <InsightList
            items={insights.hot_tables}
            label="DatabaseTable"
            tab="lineage"
            trailing={(item) => (
              <span className="flex items-center gap-1.5">
                {item.triggers ? <Badge tone="danger">trigger</Badge> : null}
                {item.declared === false ? <Badge title="No CREATE TABLE found in the repository">undeclared</Badge> : null}
                <span className="tabular-nums">
                  {item.readers ?? 0} · {item.writers ?? 0}
                </span>
              </span>
            )}
          />
        </Section>

        <Section title="Languages" aside={`${formatNumber(totalLines)} lines`}>
          <div className="p-4">
            <div className="flex h-2.5 overflow-hidden rounded-full bg-line">
              {lines.map(([language, count], index) => (
                <div
                  key={language}
                  title={`${language}: ${formatNumber(count)} lines`}
                  style={{
                    width: `${(count / totalLines) * 100}%`,
                    background: LANGUAGE_COLOURS[index % LANGUAGE_COLOURS.length],
                  }}
                />
              ))}
            </div>
            <ul className="mt-3 space-y-1.5 text-sm">
              {lines.map(([language, count], index) => (
                <li key={language} className="flex items-center gap-2">
                  <span
                    className="size-2 rounded-full"
                    style={{ background: LANGUAGE_COLOURS[index % LANGUAGE_COLOURS.length] }}
                  />
                  <span className="font-mono">{language}</span>
                  <span className="ml-auto tabular-nums text-dim">
                    {formatNumber(count)} lines · {plural(stats.languages?.[language] ?? 0, "file")}
                  </span>
                </li>
              ))}
            </ul>
            <dl className="mt-4 grid grid-cols-2 gap-2 border-t border-line pt-3 text-xs text-dim">
              <div>
                <dt className="text-faint">graph</dt>
                <dd className="font-mono text-fg">
                  {formatNumber(graph.total_nodes)} nodes · {formatNumber(graph.total_edges)} edges
                </dd>
              </div>
              <div>
                <dt className="text-faint">parse failures</dt>
                <dd className="font-mono text-fg">
                  {formatNumber(stats.files_failed ?? 0)} files · {formatNumber(stats.files_with_syntax_errors ?? 0)}{" "}
                  with syntax errors
                </dd>
              </div>
            </dl>
          </div>
        </Section>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Section title="Oldest surviving code" aside="last change">
          <InsightList
            items={insights.oldest_code}
            trailing={(item) => formatDate(item.last_modified_at)}
            subtitle={(item) => `${item.last_author ?? "unknown"} · ${locationOf(item)}`}
          />
        </Section>
        <Section title="Most complex" aside="branches">
          <InsightList items={insights.most_complex} trailing={(item) => item.complexity} />
        </Section>
        <Section title="Most called" aside="callers">
          <InsightList items={insights.most_called} trailing={(item) => item.fan_in} />
        </Section>
      </div>

      <div className="grid items-start gap-4 lg:grid-cols-3">
        <Section
          title="No caller found"
          aside={dead ? `${plural(dead.count, "function")} · ${formatNumber(dead.lines)} lines` : undefined}
        >
          {dead && dead.count > 0 ? (
            <>
              <InsightList items={dead.largest} trailing={(item) => (item.loc ? `${item.loc} lines` : null)} />
              <p className="border-t border-line px-3 py-2 text-xs text-faint">
                Candidates only: reflection, dependency injection and callers outside this repository are
                invisible to static analysis.
              </p>
            </>
          ) : (
            <Empty title="No dead-code candidate">
              Accessors, tests and anything a framework or code outside the repository could call are not
              counted, so this is not proof that everything is used.
            </Empty>
          )}
        </Section>

        <Section title="Knowledge held by one person">
          {insights.single_author_code && insights.single_author_code.owners.length > 0 ? (
            <ul className="divide-y divide-line text-sm">
              {insights.single_author_code.owners.map((owner) => (
                <li key={owner.name} className="flex items-center justify-between px-3 py-2">
                  <span>{owner.name}</span>
                  <span className="tabular-nums text-dim">{plural(owner.functions, "function")} written alone</span>
                </li>
              ))}
              <li className="px-3 py-2 text-xs text-faint">
                Functions of 15 lines or more whose surviving lines all come from one author, according to git
                blame.
              </li>
            </ul>
          ) : (
            <Empty title="No larger function has a single author">
              Counted: functions of 15 lines or more whose surviving lines all come from one author.
            </Empty>
          )}
        </Section>

        <Section title="Questions worth asking">
          {suggestions.data && suggestions.data.suggestions.length > 0 ? (
            <ul className="divide-y divide-line">
              {suggestions.data.suggestions.map((suggestion) => (
                <li key={suggestion.question}>
                  <button
                    type="button"
                    className="w-full px-3 py-2 text-left text-sm text-dim transition-colors hover:bg-hover hover:text-fg"
                    onClick={() => ask(suggestion.question, suggestion.node_id)}
                  >
                    {suggestion.question}
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <Empty title={suggestions.loading ? "Loading" : "No suggestion for this repository"} />
          )}
        </Section>
      </div>

      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn" onClick={() => open(null, "explore")}>
          Browse and search the code
        </button>
        <button type="button" className="btn" onClick={() => open(null, "map")}>
          See how the parts depend on each other
        </button>
      </div>
    </div>
  );
}

function locationOf(item: InsightItem): string {
  if (!item.path) return "";
  const name = item.path.slice(item.path.lastIndexOf("/") + 1);
  return item.line ? `${name}:${item.line}` : name;
}

function InsightList({
  items,
  label = "Function",
  tab = "explore",
  trailing,
  subtitle,
}: {
  items: InsightItem[] | undefined;
  label?: NodeBrief["label"];
  tab?: "explore" | "lineage";
  trailing?: (item: InsightItem) => React.ReactNode;
  subtitle?: (item: InsightItem) => string;
}) {
  if (!items || items.length === 0) return <Empty title="Nothing to show" />;
  return (
    <ul className="max-h-72 divide-y divide-line overflow-auto">
      {items.map((item) => (
        <li key={item.id}>
          <NodeRow
            node={{ id: item.id, label, name: item.name, location: locationOf(item) || null }}
            tab={tab}
            trailing={trailing?.(item)}
            subtitle={subtitle ? subtitle(item) : locationOf(item) || undefined}
          />
        </li>
      ))}
    </ul>
  );
}
