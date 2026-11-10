// Shapes of the Archaeo-Code HTTP API (prefix /api/v1). Kept in step with backend/serializers.py.

export type NodeLabel =
  | "Repository"
  | "File"
  | "Class"
  | "Function"
  | "DatabaseQuery"
  | "DatabaseTable"
  | "Commit"
  | "Developer";

export type RelType =
  | "CONTAINS"
  | "CALLS"
  | "INHERITS_FROM"
  | "READS_FROM_DB"
  | "MUTATES_TABLE"
  | "TOUCHED_BY"
  | "AUTHORED_BY"
  | "FIRES_TRIGGER"
  | "OBSERVED_CALL"
  | "OBSERVED_READ"
  | "OBSERVED_WRITE";

export interface SystemInfo {
  version: string;
  components: Record<string, { status: string; [key: string]: unknown }>;
  warnings: string[];
  graph_backend: string;
  job_backend: string;
  answer_mode: "llm" | "graph-report";
  llm_model: string | null;
  embedding: string | null;
  vector_backend: string | null;
  read_only: boolean;
  telemetry_enabled: boolean;
  local_sources_enabled: boolean;
  allowed_git_hosts: string[];
  supported_extensions: string[];
}

export interface Repository {
  id: string;
  name: string;
  source: string | null;
  source_kind: string | null;
  branch: string | null;
  head_commit: string | null;
  status: string | null;
  ingested_at: string | null;
  languages: string[];
  files: number;
  functions: number;
  classes: number;
  tables: number;
  lines: number;
  is_demo: boolean;
  from_snapshot: boolean;
}

export interface InsightItem {
  id: string;
  name: string;
  path?: string;
  line?: number;
  loc?: number;
  fan_in?: number;
  complexity?: number;
  last_author?: string;
  last_modified_at?: string;
  readers?: number;
  writers?: number;
  triggers?: number;
  declared?: boolean;
}

export interface Insights {
  entry_point_count?: number;
  entry_points?: Record<string, number>;
  hot_tables?: InsightItem[];
  largest?: InsightItem[];
  most_called?: InsightItem[];
  most_complex?: InsightItem[];
  oldest_code?: InsightItem[];
  undeclared_tables?: InsightItem[];
  dead_candidates?: { count: number; lines: number; largest: InsightItem[] };
  single_author_code?: { functions: number; owners: { name: string; functions: number }[] };
}

export interface RepositoryStats {
  calls?: number;
  queries?: number;
  files_parsed?: number;
  files_failed?: number;
  files_with_syntax_errors?: number;
  languages?: Record<string, number>;
  lines_by_language?: Record<string, number>;
  history?: {
    commits?: number;
    developers?: number;
    first_commit_at?: string;
    last_commit_at?: string;
  };
}

export interface RepositoryDetail extends Repository {
  warnings: string[];
  stats: RepositoryStats;
  insights: Insights;
  graph: {
    nodes: Record<string, number>;
    edges: Record<string, number>;
    total_nodes: number;
    total_edges: number;
  };
}

export interface Job {
  id: string;
  repository_id: string;
  name: string;
  source: string;
  status: "queued" | "running" | "succeeded" | "failed" | string;
  stage: string;
  /** 0..1 */
  progress: number;
  message: string;
  error: string | null;
  warnings: string[];
  active: boolean;
  created_at: string;
  finished_at: string | null;
}

/** A node as it appears in lists, search results and graph views. */
export interface NodeBrief {
  id: string;
  label: NodeLabel;
  name: string;
  qualified_name?: string;
  kind?: string;
  language?: string;
  path?: string;
  line?: number;
  start_line?: number;
  end_line?: number;
  signature?: string;
  class_name?: string;
  is_entry_point?: boolean;
  entry_kind?: string;
  http_method?: string;
  route?: string;
  is_test?: boolean;
  is_boilerplate?: boolean;
  dead_candidate?: boolean;
  complexity?: number;
  loc?: number;
  fan_in?: number;
  fan_out?: number;
  query_count?: number;
  last_modified_at?: string;
  last_author?: string;
  primary_author?: string;
  author_count?: number;
  declared?: boolean;
  operation?: string;
  dynamic?: boolean;
  function_name?: string;
  hash?: string;
  message?: string;
  authored_at?: string;
  author_name?: string;
  pr_number?: number;
  pr_title?: string;
  pr_url?: string;
  tickets?: string[];
  location: string | null;
}

/** Everything stored on a node (the node endpoint). */
export interface NodeFull extends NodeBrief {
  docstring?: string;
  decorators?: string[];
  modifiers?: string[];
  params?: string[];
  return_type?: string;
  authors?: string[];
  unresolved_calls?: string[];
  columns?: string[];
  ddl?: string;
  sql?: string;
  body?: string;
  pr_body?: string;
  bases?: string[];
  interfaces?: string[];
  fields?: string[];
  tables_read?: string[];
  tables_written?: string[];
  [key: string]: unknown;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: RelType;
  confidence: number;
  lines?: number[] | number;
  resolution?: string;
  operations?: string[];
  events?: string[];
  count?: number;
  errors?: number;
  total_ms?: number;
  max_ms?: number;
  agent?: string;
  share?: number;
}

export interface Neighbour {
  node: NodeBrief;
  edge: GraphEdge;
}

export interface NodeContext {
  node: NodeFull;
  summary: Record<string, number>;
  containers: NodeBrief[];
  members: NodeBrief[];
  queries: NodeFull[];
  callers: Neighbour[];
  callees: Neighbour[];
  reads: Neighbour[];
  writes: Neighbour[];
  readers: Neighbour[];
  writers: Neighbour[];
  triggers: Neighbour[];
  supertypes: Neighbour[];
  subtypes: Neighbour[];
  commits: Neighbour[];
  /** For a commit: the code it changed that still carries its lines. */
  touched: Neighbour[];
  observed_callers: Neighbour[];
  observed_callees: Neighbour[];
}

export interface SourceSlice {
  path: string;
  language: string | null;
  start_line: number;
  end_line: number;
  total_lines: number;
  focus: { start_line: number; end_line: number } | null;
  text: string;
  head_commit: string | null;
}

export interface SearchResult extends NodeBrief {
  match: "location" | "name" | "semantic";
  score: number | null;
}

export interface FileEntry {
  id: string;
  path: string;
  language: string | null;
  lines: number;
  is_test: boolean;
  parse_error: string | null;
}

export interface SubGraph {
  nodes: NodeBrief[];
  edges: GraphEdge[];
  truncated: boolean;
}

export interface CallChain extends SubGraph {
  root: NodeBrief;
  /** Signed distance from the root: negative = callers, positive = callees. */
  levels: Record<string, number>;
  start_ids: string[];
}

export type LineageLayer = "entry" | "logic" | "query" | "table" | "side_effect";

export interface Lineage extends SubGraph {
  root: NodeBrief;
  layers: Record<string, LineageLayer>;
}

export interface SequenceParticipant {
  id: string;
  name: string;
  kind: "class" | "table" | "routine" | "module" | string;
  path: string | null;
}

export interface SequenceStep {
  index: number;
  depth: number;
  source: string;
  target: string;
  kind: "call" | "read" | "write" | "trigger" | string;
  label: string;
  from_node: string;
  to_node: string;
  line: number | null;
  path: string | null;
  confidence: number;
  note: string | null;
}

export interface Sequence {
  root: NodeBrief;
  participants: SequenceParticipant[];
  steps: SequenceStep[];
  truncated: boolean;
}

export interface TopologyGroup {
  id: string;
  name: string;
  kind: "directory" | "table" | string;
  files: number;
  functions: number;
  classes: number;
  lines: number;
  entry_points: number;
  languages: Record<string, number>;
}

export interface TopologyLink {
  source: string;
  target: string;
  kind: "calls" | "reads" | "writes" | "triggers" | string;
  weight: number;
  observed: number;
}

export interface Topology {
  depth: number;
  groups: TopologyGroup[];
  links: TopologyLink[];
}

export interface ObservedEdge extends GraphEdge {
  mean_ms: number | null;
  in_static_graph: boolean;
  static_confidence: number | null;
  static_resolution: string | null;
}

export interface TelemetrySummary {
  totals: {
    observed_edges: number;
    observed_calls: number;
    observed_data_access: number;
    invocations: number;
    static_calls: number;
    confirmed_static_calls: number;
    runtime_only_calls: number;
    never_observed_functions: number;
  };
  sources: string[];
  simulated: boolean;
  last_seen: string | null;
  top_calls: ObservedEdge[];
  runtime_only: ObservedEdge[];
  confirmed_guesses: ObservedEdge[];
  data_access: ObservedEdge[];
  never_observed: NodeBrief[];
  nodes: Record<string, NodeBrief>;
}

export interface Suggestion {
  question: string;
  node_id: string | null;
}

export interface EvidenceFact {
  kind: string;
  text: string;
}

export interface Evidence {
  id: string;
  kind: "code" | "sql" | "commit" | string;
  role: string;
  node_id: string;
  label: NodeLabel;
  title: string;
  citation: string | null;
  path: string | null;
  start_line: number | null;
  end_line: number | null;
  score: number;
  reason: string;
  facts: EvidenceFact[];
  text: string;
}

export interface Citation {
  raw: string;
  kind: "code" | "commit" | string;
  status: "verified" | "outside-context" | "unresolved";
  path: string | null;
  line: number | null;
  end_line: number | null;
  commit: string | null;
  node_id: string | null;
  evidence_id: string | null;
  detail: string | null;
}

export interface CitationSummary {
  total: number;
  verified: number;
  outside_context: number;
  unresolved: number;
  grounded: boolean;
}

export type ExplainEvent =
  | {
      type: "context";
      mode: "llm" | "graph-report";
      model: string | null;
      seeds: string[];
      notes: string[];
      evidence: Evidence[];
    }
  | { type: "token"; text: string }
  | { type: "warning"; message: string }
  | { type: "citations"; citations: Citation[]; summary: CitationSummary }
  | { type: "done"; answer: string; mode: string; model: string | null; warnings: string[] }
  | { type: "error"; message: string };
