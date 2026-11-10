"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "./api";

export interface Resource<T> {
  /** The value for the current key; undefined while it loads or after a failure. */
  data: T | undefined;
  /** The last value that loaded, kept while a newer request is in flight. */
  latest: T | undefined;
  error: ApiError | undefined;
  loading: boolean;
  reload: () => void;
}

interface Settled<T> {
  stamp: string;
  data?: T;
  error?: ApiError;
}

function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  return new ApiError(0, error instanceof Error ? error.message : "Unexpected error");
}

/**
 * Load a value identified by `key` (null = nothing to load). The request is aborted
 * when the key changes or the component unmounts, so a slow response can never
 * overwrite a newer one.
 */
export function useResource<T>(key: string | null, load: (signal: AbortSignal) => Promise<T>): Resource<T> {
  const [settled, setSettled] = useState<Settled<T> | null>(null);
  const [latest, setLatest] = useState<T | undefined>(undefined);
  const [version, setVersion] = useState(0);
  const loader = useRef(load);

  useEffect(() => {
    loader.current = load;
  });

  const stamp = key === null ? null : `${key}#${version}`;

  useEffect(() => {
    if (stamp === null) return;
    const controller = new AbortController();
    loader.current(controller.signal).then(
      (data) => {
        if (controller.signal.aborted) return;
        setSettled({ stamp, data });
        setLatest(data);
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setSettled({ stamp, error: toApiError(error) });
      },
    );
    return () => controller.abort();
  }, [stamp]);

  const reload = useCallback(() => setVersion((current) => current + 1), []);
  const current = settled !== null && settled.stamp === stamp ? settled : null;
  return {
    data: current?.data,
    latest: stamp === null ? undefined : (current?.data ?? latest),
    error: current?.error,
    loading: stamp !== null && current === null,
    reload,
  };
}

/** Call `tick` every `ms` milliseconds while `active`. */
export function useInterval(tick: () => void, ms: number, active: boolean): void {
  const callback = useRef(tick);
  useEffect(() => {
    callback.current = tick;
  });
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => callback.current(), ms);
    return () => window.clearInterval(timer);
  }, [ms, active]);
}

/** A value that follows `value` after it has been stable for `ms` milliseconds. */
export function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), ms);
    return () => window.clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}
