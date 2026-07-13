import { useEffect, useState } from "react";
import { ApiEnvelope } from "./api";
import { LruTtlCache } from "./pollingCache";

export type LoadState<T> = {
  envelope?: ApiEnvelope<T>;
  loading: boolean;
  refreshing: boolean;
  error?: string;
  updatedAt?: number;
  refresh: () => void;
};

export type PollingOptions = {
  cache?: boolean;
};

type PollingState<T> = {
  key: string;
  envelope?: ApiEnvelope<T>;
  loading: boolean;
  refreshing: boolean;
  error?: string;
  updatedAt?: number;
};

const responseCache = new LruTtlCache<ApiEnvelope<unknown>>(50, 5 * 60 * 1000);
const inFlight = new Map<string, Promise<ApiEnvelope<unknown>>>();

export function resetPollingStateForTests(): void {
  responseCache.clear();
  inFlight.clear();
}

export function usePolling<T>(
  key: string,
  loader: () => Promise<ApiEnvelope<T>>,
  intervalMs: number,
  enabled = true,
  options: PollingOptions = {}
): LoadState<T> {
  const cacheEnabled = options.cache !== false;
  const [state, setState] = useState<PollingState<T>>(() => {
    const cached = cacheEnabled ? responseCache.get(key) as ApiEnvelope<T> | undefined : undefined;
    return {
      key,
      envelope: cached,
      loading: enabled && !cached,
      refreshing: false
    };
  });
  const [tick, setTick] = useState(0);
  const currentState: PollingState<T> = state.key === key
    ? state
    : { key, loading: enabled, refreshing: false };

  useEffect(() => {
    let cancelled = false;
    if (!enabled) {
      setState((current) => current.key === key
        ? { ...current, loading: false, refreshing: false }
        : { key, loading: false, refreshing: false });
      return;
    }
    const cached = cacheEnabled ? responseCache.get(key) as ApiEnvelope<T> | undefined : undefined;
    setState((current) => {
      const currentEnvelope = current.key === key ? current.envelope : undefined;
      const envelope = cached ?? currentEnvelope;
      return {
        key,
        envelope,
        loading: !envelope,
        refreshing: Boolean(envelope),
        updatedAt: current.key === key ? current.updatedAt : undefined
      };
    });
    const existing = inFlight.get(key) as Promise<ApiEnvelope<T>> | undefined;
    const request = existing ?? loader();
    if (!existing) {
      inFlight.set(key, request as Promise<ApiEnvelope<unknown>>);
    }
    request
      .then((result) => {
        if (!cancelled) {
          if (cacheEnabled) {
            responseCache.set(key, result as ApiEnvelope<unknown>);
          }
          setState({
            key,
            envelope: result,
            loading: false,
            refreshing: false,
            updatedAt: Date.now()
          });
        }
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setState((current) => ({
            ...(current.key === key ? current : { key, loading: false, refreshing: false }),
            loading: false,
            refreshing: false,
            error: caught instanceof Error ? caught.message : String(caught)
          }));
        }
      })
      .finally(() => {
        if (inFlight.get(key) === request) {
          inFlight.delete(key);
        }
      });
    return () => {
      cancelled = true;
    };
    // key and tick intentionally drive reloads; loader is recreated by caller with current filters.
  }, [key, tick, enabled, cacheEnabled]);

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
    envelope: currentState.envelope,
    loading: currentState.loading,
    refreshing: currentState.refreshing,
    error: currentState.error,
    updatedAt: currentState.updatedAt,
    refresh: () => setTick((value) => value + 1)
  };
}
