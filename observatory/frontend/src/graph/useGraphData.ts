import { useCallback, useEffect, useRef, useState } from "react";
import { fetchChildren, fetchGraph } from "../api";
import type { GraphResponse, ID } from "../types";
import { applyFocus, mergeExpansion, type ExpandedGroups } from "./assemble";

export interface GraphSeed {
  entity_type?: string;
  entity_id?: string | number;
  run_id?: number;
}

const POLL_INTERVAL_MS = 4000;
const ACTIVE_STATUSES = new Set(["running", "pending", "started", "in_progress"]);

export function useGraphData(seed: GraphSeed | null) {
  const [base, setBase] = useState<GraphResponse | null>(null);
  const [expanded, setExpanded] = useState<ExpandedGroups>({});
  const [focusMode, setFocusMode] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seedKey = seed ? JSON.stringify(seed) : null;
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const reload = useCallback(async () => {
    if (!seed) return;
    setLoading(true);
    setError(null);
    try {
      const g = await fetchGraph(seed);
      setBase(g);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [seedKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setExpanded({});
    setFocusMode(false);
    reload();
  }, [seedKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // Lightweight polling: refresh whenever the current graph has a node whose
  // status looks active/incomplete, so a still-running mission/generation
  // updates without a manual reload. Idle graphs (everything finished) don't
  // poll at all.
  useEffect(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (!base) return;
    const hasActive = base.nodes.some((n) => n.status && ACTIVE_STATUSES.has(n.status));
    if (!hasActive) return;
    pollRef.current = setInterval(reload, POLL_INTERVAL_MS);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [base, reload]);

  const expandGroup = useCallback(async (groupId: string) => {
    if (expanded[groupId]) return; // already fetched -- toggle is handled by the caller's own open/closed state
    const { children } = await fetchChildren({ group: groupId });
    setExpanded((prev) => ({ ...prev, [groupId]: children }));
  }, [expanded]);

  const collapseGroup = useCallback((groupId: string) => {
    setExpanded((prev) => {
      const next = { ...prev };
      delete next[groupId];
      return next;
    });
  }, []);

  const expandAll = useCallback(async () => {
    if (!base) return;
    await Promise.all(base.groups.map((g) => expandGroup(g.group)));
  }, [base, expandGroup]);

  const merged = base ? mergeExpansion(base, expanded) : { nodes: [], edges: [] };
  const focused = base ? applyFocus(merged, base.emphasized_path, focusMode) : { nodes: [], edges: [], hiddenCount: 0 };

  return {
    base, loading, error, expanded, focusMode, setFocusMode,
    expandGroup, collapseGroup, expandAll, reload,
    display: focused,
    emphasizedPath: base?.emphasized_path ?? ([] as ID[]),
    focusNodeId: base?.focus_node_id ?? null,
  };
}
