import { QueryClient, type QueryKey } from "@tanstack/react-query";

export const QUERY_GC_TIME_MS = 5 * 60 * 1000;
export const QUERY_CACHE_MAX_ENTRIES = 50;

export function normalizeApiPath(path: string): string {
  const isAbsolute = /^[a-z][a-z\d+.-]*:\/\//i.test(path);
  const url = new URL(path, "http://clipper.local");
  const sorted = Array.from(url.searchParams.entries()).sort(([leftKey, leftValue], [rightKey, rightValue]) => {
    const keyOrder = leftKey.localeCompare(rightKey);
    return keyOrder === 0 ? leftValue.localeCompare(rightValue) : keyOrder;
  });
  const search = new URLSearchParams(sorted).toString();
  const resource = `${url.pathname}${search ? `?${search}` : ""}`;
  return isAbsolute ? `${url.origin}${resource}` : resource;
}

export function apiQueryKey(path: string): readonly ["api", string] {
  return ["api", normalizeApiPath(path)] as const;
}

export const appQueryClient = new QueryClient({
  defaultOptions: {
    queries: {
      gcTime: QUERY_GC_TIME_MS,
      retry: 2,
      retryDelay: (attempt) => Math.min(500 * 2 ** attempt, 5_000),
      refetchOnWindowFocus: true,
      refetchIntervalInBackground: false
    }
  }
});

export function pruneQueryCache(
  client: QueryClient,
  maxEntries = QUERY_CACHE_MAX_ENTRIES
): void {
  const queries = client.getQueryCache().getAll();
  const excess = queries.length - Math.max(1, maxEntries);
  if (excess <= 0) {
    return;
  }
  const evictionOrder = [...queries].sort((left, right) => {
    const leftActive = left.getObserversCount() > 0 ? 1 : 0;
    const rightActive = right.getObserversCount() > 0 ? 1 : 0;
    if (leftActive !== rightActive) {
      return leftActive - rightActive;
    }
    return left.state.dataUpdatedAt - right.state.dataUpdatedAt;
  });
  evictionOrder.slice(0, excess).forEach((query) => {
    client.removeQueries({ queryKey: query.queryKey, exact: true });
  });
}

let pruneScheduled = false;
appQueryClient.getQueryCache().subscribe(() => {
  if (pruneScheduled || appQueryClient.getQueryCache().getAll().length <= QUERY_CACHE_MAX_ENTRIES) {
    return;
  }
  pruneScheduled = true;
  queueMicrotask(() => {
    try {
      pruneQueryCache(appQueryClient);
    } finally {
      pruneScheduled = false;
    }
  });
});

function apiPathFromKey(key: QueryKey): string | undefined {
  return key[0] === "api" && typeof key[1] === "string" ? key[1] : undefined;
}

export function invalidationPrefixesForMutation(path: string): string[] {
  const normalized = normalizeApiPath(path);
  const prefixes = new Set<string>(["/api/control/jobs", "/api/overview"]);

  if (normalized.startsWith("/api/control/queue")) {
    prefixes.add("/api/dashboard");
    prefixes.add("/api/queue");
  }
  if (normalized.startsWith("/api/operations/rescore")) {
    prefixes.add("/api/scores");
  }
  if (normalized.startsWith("/api/operations/compliance")) {
    prefixes.add("/api/compliance");
  }
  if (normalized.startsWith("/api/modules") || normalized.startsWith("/api/operations/module-assembly")) {
    prefixes.add("/api/modules");
  }
  if (normalized.startsWith("/api/settings")) {
    prefixes.add("/api/settings");
    prefixes.add("/api/system");
  }
  if (normalized.startsWith("/api/variations")) {
    prefixes.add("/api/variations");
  }

  return Array.from(prefixes);
}

export function invalidateApiDataForMutation(path: string): Promise<void> {
  const prefixes = invalidationPrefixesForMutation(path);
  return appQueryClient.invalidateQueries({
    predicate: (candidate) => {
      const candidatePath = apiPathFromKey(candidate.queryKey);
      return Boolean(candidatePath && prefixes.some((prefix) => candidatePath.startsWith(prefix)));
    }
  });
}

export function invalidateApiPrefix(prefix: string): Promise<void> {
  const normalized = normalizeApiPath(prefix);
  return appQueryClient.invalidateQueries({
    predicate: (candidate) => apiPathFromKey(candidate.queryKey)?.startsWith(normalized) ?? false
  });
}
