// @vitest-environment jsdom

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiEnvelope } from "./api";
import { useApiQuery } from "./useApiQuery";

function envelope<T>(data: T): ApiEnvelope<T> {
  return { data, generated_at: "2026-07-13T00:00:00Z", source_signatures: [], warnings: [] };
}

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useApiQuery", () => {
  it("deduplicates canonical paths across consumers", async () => {
    const fetchMock = vi.fn(async (_path: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(envelope("ok")), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => ({
      first: useApiQuery<string>("/api/example?b=2&a=1", false),
      second: useApiQuery<string>("/api/example?a=1&b=2", false)
    }), { wrapper: wrapper() });

    await waitFor(() => expect(result.current.first.envelope?.data).toBe("ok"));
    expect(result.current.second.envelope?.data).toBe("ok");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/example?a=1&b=2");
  });

  it("cancels a superseded request and releases the previous envelope", async () => {
    let firstSignal: AbortSignal | undefined;
    const fetchMock = vi.fn((path: RequestInfo | URL, init?: RequestInit) => {
      if (String(path).includes("first")) {
        firstSignal = init?.signal ?? undefined;
        return new Promise<Response>((_resolve, reject) => {
          firstSignal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        });
      }
      return Promise.resolve(new Response(JSON.stringify(envelope("second")), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }));
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result, rerender } = renderHook(
      ({ id }) => useApiQuery<string>(`/api/jobs/${id}`, false, true, { cache: false }),
      { initialProps: { id: "first" }, wrapper: wrapper() }
    );

    await waitFor(() => expect(firstSignal).toBeDefined());
    rerender({ id: "second" });
    expect(result.current.envelope).toBeUndefined();
    await waitFor(() => expect(result.current.envelope?.data).toBe("second"));
    expect(firstSignal?.aborted).toBe(true);
  });

  it("stops polling when an interval resolver returns false", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(envelope({ status: "completed" })), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(
      () => useApiQuery<{ status: string }>("/api/jobs/terminal", (job) => job?.status === "running" ? 2_000 : false),
      { wrapper: wrapper() }
    );
    await act(async () => { await vi.runAllTimersAsync(); });
    expect(result.current.envelope?.data.status).toBe("completed");
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
