import { describe, expect, it } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import {
  apiQueryKey,
  appQueryClient,
  invalidationPrefixesForMutation,
  normalizeApiPath,
  pruneQueryCache,
  QUERY_CACHE_MAX_ENTRIES
} from "./queryClient";

describe("API query keys", () => {
  it("normalizes query parameters so equivalent requests share a key", () => {
    expect(normalizeApiPath("/api/scores?status=ready&offset=0&product=tea"))
      .toBe("/api/scores?offset=0&product=tea&status=ready");
    expect(apiQueryKey("/api/scores?product=tea&status=ready&offset=0"))
      .toEqual(["api", "/api/scores?offset=0&product=tea&status=ready"]);
  });

  it("maps mutations to the affected compact and domain queries", () => {
    expect(invalidationPrefixesForMutation("/api/control/queue")).toEqual(expect.arrayContaining([
      "/api/control/jobs",
      "/api/overview",
      "/api/dashboard",
      "/api/queue"
    ]));
    expect(invalidationPrefixesForMutation("/api/modules/m-1/review")).toContain("/api/modules");
    expect(invalidationPrefixesForMutation("/api/operations/compliance-scan")).toContain("/api/compliance");
  });

  it("pauses interval work while hidden and refetches on visibility return", () => {
    const defaults = appQueryClient.getDefaultOptions().queries;
    expect(defaults?.refetchIntervalInBackground).toBe(false);
    expect(defaults?.refetchOnWindowFocus).toBe(true);
    expect(defaults?.gcTime).toBe(300_000);
    expect(defaults?.retry).toBe(2);
  });

  it("evicts the oldest inactive entries above the renderer cache limit", () => {
    const client = new QueryClient();
    for (let index = 0; index <= QUERY_CACHE_MAX_ENTRIES; index += 1) {
      client.setQueryData(["api", `/api/test/${index}`], index, { updatedAt: index + 1 });
    }

    pruneQueryCache(client);

    expect(client.getQueryCache().getAll()).toHaveLength(QUERY_CACHE_MAX_ENTRIES);
    expect(client.getQueryData(["api", "/api/test/0"])).toBeUndefined();
    expect(client.getQueryData(["api", `/api/test/${QUERY_CACHE_MAX_ENTRIES}`])).toBe(QUERY_CACHE_MAX_ENTRIES);
  });
});
