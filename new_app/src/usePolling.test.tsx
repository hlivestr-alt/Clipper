// @vitest-environment jsdom

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiEnvelope } from "./api";
import { resetPollingStateForTests, usePolling } from "./usePolling";

function envelope(value: string): ApiEnvelope<string> {
  return {
    data: value,
    generated_at: "2026-07-11T00:00:00Z",
    source_signatures: [],
    warnings: []
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((innerResolve) => {
    resolve = innerResolve;
  });
  return { promise, resolve };
}

describe("usePolling", () => {
  beforeEach(() => {
    resetPollingStateForTests();
  });

  it("does not expose the previous request envelope after the key changes", async () => {
    const first = deferred<ApiEnvelope<string>>();
    const second = deferred<ApiEnvelope<string>>();
    const { result, rerender } = renderHook(
      ({ cacheKey, loader }: { cacheKey: string; loader: () => Promise<ApiEnvelope<string>> }) =>
        usePolling(cacheKey, loader, 0, true, { cache: false }),
      { initialProps: { cacheKey: "job-detail:first", loader: () => first.promise } }
    );

    await act(async () => first.resolve(envelope("first")));
    expect(result.current.envelope?.data).toBe("first");

    rerender({ cacheKey: "job-detail:second", loader: () => second.promise });
    expect(result.current.envelope).toBeUndefined();
    expect(result.current.loading).toBe(true);

    await act(async () => second.resolve(envelope("second")));
    expect(result.current.envelope?.data).toBe("second");
  });

  it("does not retain responses when caching is disabled", async () => {
    const loader = vi.fn(async () => envelope("detail"));
    const first = renderHook(() => usePolling("job-detail:one", loader, 0, true, { cache: false }));
    await waitFor(() => expect(first.result.current.envelope?.data).toBe("detail"));
    await act(async () => Promise.resolve());
    first.unmount();

    const second = renderHook(() => usePolling("job-detail:one", loader, 0, true, { cache: false }));
    await waitFor(() => expect(second.result.current.envelope?.data).toBe("detail"));

    expect(loader).toHaveBeenCalledTimes(2);
    second.unmount();
  });
});
