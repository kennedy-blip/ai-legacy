"use client";

import { createContext, useContext } from "react";

import type { RepositoryDetail, SystemInfo } from "@/lib/types";

export const TABS = [
  { id: "overview", label: "Overview" },
  { id: "explore", label: "Explore" },
  { id: "flow", label: "Sequence" },
  { id: "lineage", label: "Data lineage" },
  { id: "map", label: "Topology" },
  { id: "runtime", label: "Runtime" },
  { id: "ask", label: "Ask" },
] as const;

export type TabId = (typeof TABS)[number]["id"];

export function isTab(value: string | null): value is TabId {
  return TABS.some((tab) => tab.id === value);
}

export interface WorkspaceValue {
  repositoryId: string;
  repository: RepositoryDetail;
  system: SystemInfo | undefined;
  /** The node the user is looking at, shared by every tab. */
  nodeId: string | null;
  /** Select a node and optionally switch tab; `null` clears the selection. */
  open: (nodeId: string | null, tab?: TabId) => void;
  /** Ask a question in the Ask tab, optionally about a node. */
  ask: (question: string, nodeId?: string | null) => void;
  /** Reload the repository record (after telemetry changes, for example). */
  refresh: () => void;
}

const WorkspaceContext = createContext<WorkspaceValue | null>(null);

export const WorkspaceProvider = WorkspaceContext.Provider;

export function useWorkspace(): WorkspaceValue {
  const value = useContext(WorkspaceContext);
  if (value === null) throw new Error("useWorkspace must be used inside the repository workspace");
  return value;
}
