import { useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ApiEnvelope } from "./api";
import { getJson } from "./api";
import { apiQueryKey, normalizeApiPath, QUERY_GC_TIME_MS } from "./queryClient";
import { getLiveUpdateSnapshot, shouldPollWhileLive, subscribeLiveUpdates } from "./liveUpdates";

export type LoadState<T> = {
  envelope?: ApiEnvelope<T>;
  loading: boolean;
  refreshing: boolean;
  error?: string;
  updatedAt?: number;
  refresh: () => void;
};

export type ApiQueryInterval<T> = number | false | ((data: T | undefined) => number | false);

export type ApiQueryOptions = {
  cache?: boolean;
};

export function useApiQuery<T>(
  path: string,
  intervalMs: ApiQueryInterval<T>,
  enabled = true,
  options: ApiQueryOptions = {}
): LoadState<T> {
  const normalizedPath = normalizeApiPath(path);
  const liveUpdates = useSyncExternalStore(
    subscribeLiveUpdates,
    getLiveUpdateSnapshot,
    getLiveUpdateSnapshot
  );
  const result = useQuery({
    queryKey: apiQueryKey(normalizedPath),
    queryFn: ({ signal }) => getJson<T>(normalizedPath, { signal }),
    enabled,
    gcTime: options.cache === false ? 0 : QUERY_GC_TIME_MS,
    refetchInterval: (queryState) => {
      if (liveUpdates.connected && !shouldPollWhileLive(normalizedPath)) {
        return false;
      }
      const interval = typeof intervalMs === "function"
        ? intervalMs(queryState.state.data?.data)
        : intervalMs;
      return interval && interval > 0 ? interval : false;
    },
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true
  });

  return {
    envelope: result.data,
    loading: enabled && result.isPending,
    refreshing: result.isFetching && !result.isPending,
    error: result.error instanceof Error ? result.error.message : result.error ? String(result.error) : undefined,
    updatedAt: result.dataUpdatedAt || undefined,
    refresh: () => {
      void result.refetch();
    }
  };
}
