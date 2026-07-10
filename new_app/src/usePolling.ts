import { useEffect, useState } from "react";
import { ApiEnvelope } from "./api";

export type LoadState<T> = {
  envelope?: ApiEnvelope<T>;
  loading: boolean;
  refreshing: boolean;
  error?: string;
  updatedAt?: number;
  refresh: () => void;
};

const responseCache = new Map<string, ApiEnvelope<unknown>>();
const inFlight = new Map<string, Promise<ApiEnvelope<unknown>>>();

export function usePolling<T>(
  key: string,
  loader: () => Promise<ApiEnvelope<T>>,
  intervalMs: number,
  enabled = true
): LoadState<T> {
  const [envelope, setEnvelope] = useState<ApiEnvelope<T> | undefined>(() => responseCache.get(key) as ApiEnvelope<T> | undefined);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string>();
  const [updatedAt, setUpdatedAt] = useState<number>();
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    if (!enabled) {
      return;
    }
    const cached = responseCache.get(key) as ApiEnvelope<T> | undefined;
    if (cached && envelope === undefined) {
      setEnvelope(cached);
      setLoading(false);
    }
    setLoading((current) => current && !cached && envelope === undefined);
    setRefreshing(Boolean(cached || envelope));
    const existing = inFlight.get(key) as Promise<ApiEnvelope<T>> | undefined;
    const request = existing ?? loader();
    if (!existing) {
      inFlight.set(key, request as Promise<ApiEnvelope<unknown>>);
    }
    request
      .then((result) => {
        if (!cancelled) {
          responseCache.set(key, result as ApiEnvelope<unknown>);
          setEnvelope(result);
          setError(undefined);
          setUpdatedAt(Date.now());
        }
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : String(caught));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
          setRefreshing(false);
        }
        if (inFlight.get(key) === request) {
          inFlight.delete(key);
        }
      });
    return () => {
      cancelled = true;
    };
    // key and tick intentionally drive reloads; loader is recreated by caller with current filters.
  }, [key, tick, enabled]);

  useEffect(() => {
    if (!enabled || intervalMs <= 0) {
      return;
    }
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        setTick((value) => value + 1);
      }
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [enabled, intervalMs]);

  return {
    envelope,
    loading,
    refreshing,
    error,
    updatedAt,
    refresh: () => setTick((value) => value + 1)
  };
}
