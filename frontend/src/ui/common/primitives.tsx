import type { ReactNode } from "react";

import type { ApiError } from "@/lib/api";
import { TONE_TEXT, type Tone } from "@/lib/format";

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-dim" role="status">
      <span className="size-3.5 animate-spin rounded-full border-2 border-line-strong border-t-accent" />
      {label}
    </span>
  );
}

export function Centered({ children }: { children: ReactNode }) {
  return <div className="flex min-h-40 flex-1 items-center justify-center p-8 text-center">{children}</div>;
}

export function ErrorNote({ error, onRetry }: { error: ApiError | Error; onRetry?: () => void }) {
  return (
    <div className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger" role="alert">
      <span>{error.message}</span>
      {onRetry ? (
        <button type="button" className="ml-3 underline underline-offset-2" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <Centered>
      <div className="max-w-md">
        <p className="font-medium text-fg">{title}</p>
        {children ? <div className="mt-1 text-sm text-dim">{children}</div> : null}
      </div>
    </Centered>
  );
}

const BADGE_TONES: Record<Tone | "accent", string> = {
  fn: "border-fn/40 text-fn",
  cls: "border-cls/40 text-cls",
  tbl: "border-tbl/40 text-tbl",
  qry: "border-qry/40 text-qry",
  hist: "border-hist/40 text-hist",
  danger: "border-danger/40 text-danger",
  runtime: "border-runtime/40 text-runtime",
  dim: "border-line-strong text-dim",
  accent: "border-accent/50 bg-accent/10 text-accent",
};

export function Badge({
  tone = "dim",
  title,
  children,
}: {
  tone?: Tone | "accent";
  title?: string;
  children: ReactNode;
}) {
  return (
    <span
      title={title}
      className={`inline-flex shrink-0 items-center rounded border px-1.5 py-px text-[11px] font-medium leading-4 ${BADGE_TONES[tone]}`}
    >
      {children}
    </span>
  );
}

export function Dot({ tone }: { tone: Tone }) {
  return <span className={`inline-block size-2 shrink-0 rounded-full bg-current ${TONE_TEXT[tone]}`} />;
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="panel px-4 py-3" title={hint}>
      <div className="eyebrow">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums tracking-tight">{value}</div>
    </div>
  );
}

export function Section({
  title,
  aside,
  children,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="panel flex min-w-0 flex-col">
      <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
        <h2 className="text-sm font-semibold">{title}</h2>
        {aside ? <div className="text-xs text-faint">{aside}</div> : null}
      </header>
      <div className="min-w-0 flex-1">{children}</div>
    </section>
  );
}
