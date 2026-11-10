// Typed client for the Archaeo-Code API. Every call is same-origin: the backend is
// reached through /api/v1 (a platform route on Vercel, a Next rewrite elsewhere).

import type {
  CallChain,
  ExplainEvent,
  FileEntry,
  Job,
  Lineage,
  NodeBrief,
  NodeContext,
  Repository,
  RepositoryDetail,
  SearchResult,
  Sequence,
  SourceSlice,
  Suggestion,
  SystemInfo,
  TelemetrySummary,
  Topology,
} from "./types";

const BASE = "/api/v1";
const TOKEN_KEY = "archaeo.api-token";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function getToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage can be unavailable (private mode); the token then lasts for this page only.
  }
}

function headers(json: boolean): HeadersInit {
  const result: Record<string, string> = { Accept: "application/json" };
  if (json) result["Content-Type"] = "application/json";
  const token = typeof window === "undefined" ? null : getToken();
  if (token) result.Authorization = `Bearer ${token}`;
  return result;
}

function describe(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{loc: [...], msg: "..."}]
    return detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) {
          const location = Array.isArray(item.loc) ? item.loc.slice(1).join(".") : "";
          return location ? `${location}: ${String(item.msg)}` : String(item.msg);
        }
        return String(item);
      })
      .join("; ");
  }
  if (detail && typeof detail === "object" && "message" in detail) return String(detail.message);
  return fallback;
}

async function failure(response: Response): Promise<ApiError> {
  let message = `${response.status} ${response.statusText}`.trim();
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) message = describe(body.detail, message);
  } catch {
    // Not JSON (a proxy error page, for example): keep the status line.
  }
  return new ApiError(response.status, message);
}

async function request<T>(path: string, init: RequestInit & { signal?: AbortSignal } = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(BASE + path, {
      ...init,
      headers: { ...headers(init.body !== undefined), ...init.headers },
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "The Archaeo-Code API cannot be reached.");
  }
  if (!response.ok) throw await failure(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

const repo = (id: string) => `/repositories/${encodeURIComponent(id)}`;

export const api = {
  system: (signal?: AbortSignal) => request<SystemInfo>("/system", { signal }),

  repositories: (signal?: AbortSignal) => request<Repository[]>("/repositories", { signal }),
  repository: (id: string, signal?: AbortSignal) => request<RepositoryDetail>(repo(id), { signal }),
  ingest: (body: { source: string; name?: string; branch?: string; token?: string }) =>
    request<Job>("/repositories", { method: "POST", body: JSON.stringify(body) }),
  reingest: (id: string) => request<Job>(`${repo(id)}/reingest`, { method: "POST", body: "{}" }),
  remove: (id: string) => request<void>(repo(id), { method: "DELETE" }),
  job: (id: string, signal?: AbortSignal) => request<Job>(`/jobs/${encodeURIComponent(id)}`, { signal }),
  jobs: (repositoryId?: string, signal?: AbortSignal) =>
    request<Job[]>(`/jobs${query({ repository_id: repositoryId, limit: 10 })}`, { signal }),

  search: (id: string, q: string, options: { semantic?: boolean; limit?: number } = {}, signal?: AbortSignal) =>
    request<{ query: string; results: SearchResult[] }>(
      `${repo(id)}/search${query({ q, semantic: options.semantic ?? true, limit: options.limit ?? 30 })}`,
      { signal },
    ),
  files: (id: string, signal?: AbortSignal) =>
    request<{ files: FileEntry[]; has_more: boolean }>(`${repo(id)}/files`, { signal }),
  entryPoints: (id: string, signal?: AbortSignal) =>
    request<{ entry_points: NodeBrief[]; has_more: boolean }>(`${repo(id)}/entry-points`, { signal }),
  node: (id: string, nodeId: string, signal?: AbortSignal) =>
    request<NodeContext>(`${repo(id)}/nodes/${encodeURIComponent(nodeId)}`, { signal }),
  nodeSource: (id: string, nodeId: string, signal?: AbortSignal) =>
    request<SourceSlice>(`${repo(id)}/nodes/${encodeURIComponent(nodeId)}/source${query({ context: 6 })}`, {
      signal,
    }),
  fileSource: (id: string, path: string, range: { start?: number; end?: number } = {}, signal?: AbortSignal) =>
    request<SourceSlice>(`${repo(id)}/source${query({ path, start: range.start, end: range.end })}`, { signal }),

  callChain: (
    id: string,
    nodeId: string,
    options: { depth?: number; direction?: "upstream" | "downstream" | "both"; runtime?: boolean } = {},
    signal?: AbortSignal,
  ) =>
    request<CallChain>(
      `${repo(id)}/call-chain${query({
        node_id: nodeId,
        depth: options.depth ?? 3,
        direction: options.direction ?? "both",
        include_runtime: options.runtime ?? false,
      })}`,
      { signal },
    ),
  sequence: (id: string, nodeId: string, signal?: AbortSignal) =>
    request<Sequence>(`${repo(id)}/sequence${query({ node_id: nodeId })}`, { signal }),
  lineage: (id: string, nodeId: string, signal?: AbortSignal) =>
    request<Lineage>(`${repo(id)}/lineage${query({ node_id: nodeId })}`, { signal }),
  topology: (id: string, signal?: AbortSignal) => request<Topology>(`${repo(id)}/topology`, { signal }),

  suggestions: (id: string, signal?: AbortSignal) =>
    request<{ suggestions: Suggestion[] }>(`${repo(id)}/suggestions`, { signal }),

  telemetry: (id: string, signal?: AbortSignal) =>
    request<TelemetrySummary>(`${repo(id)}/telemetry/summary`, { signal }),
  simulate: (id: string, traces: number) =>
    request<{ received: number; accepted: number; edges_updated: number; traces: number }>(
      `${repo(id)}/telemetry/simulate`,
      { method: "POST", body: JSON.stringify({ traces }) },
    ),
  clearTelemetry: (id: string) => request<{ removed: number }>(`${repo(id)}/telemetry`, { method: "DELETE" }),
};

export interface ExplainRequest {
  question: string;
  focus_node_id?: string | null;
  history?: { role: "user" | "assistant"; content: string }[];
}

/** Ask a question; events arrive as the server produces them (newline-delimited JSON). */
export async function* explain(
  repositoryId: string,
  body: ExplainRequest,
  signal?: AbortSignal,
): AsyncGenerator<ExplainEvent> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${repo(repositoryId)}/explain`, {
      method: "POST",
      headers: { ...headers(true), Accept: "application/x-ndjson" },
      body: JSON.stringify({ ...body, stream: true }),
      signal,
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "The Archaeo-Code API cannot be reached.");
  }
  if (!response.ok) throw await failure(response);
  if (!response.body) throw new ApiError(0, "The server sent an empty response.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffered += decoder.decode(value, { stream: true });
      let newline = buffered.indexOf("\n");
      while (newline >= 0) {
        const line = buffered.slice(0, newline).trim();
        buffered = buffered.slice(newline + 1);
        if (line) yield JSON.parse(line) as ExplainEvent;
        newline = buffered.indexOf("\n");
      }
    }
    const rest = (buffered + decoder.decode()).trim();
    if (rest) yield JSON.parse(rest) as ExplainEvent;
  } finally {
    reader.releaseLock();
  }
}
