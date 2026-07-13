// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";
import { getJson } from "./api";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("getJson", () => {
  it("aborts reads after the configured timeout", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn((_path: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(init.signal?.reason), { once: true });
    }));
    vi.stubGlobal("fetch", fetchMock);

    const request = getJson("/api/slow", { timeoutMs: 30_000 });
    const rejection = expect(request).rejects.toMatchObject({ name: "TimeoutError" });
    await vi.advanceTimersByTimeAsync(30_000);

    await rejection;
    expect((fetchMock.mock.calls[0]?.[1]?.signal as AbortSignal).aborted).toBe(true);
  });

  it("forwards caller cancellation to fetch", async () => {
    const caller = new AbortController();
    let receivedSignal: AbortSignal | undefined;
    const fetchMock = vi.fn((_path: RequestInfo | URL, init?: RequestInit) => {
      receivedSignal = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        receivedSignal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const request = getJson("/api/cancel", { signal: caller.signal });
    const rejection = expect(request).rejects.toMatchObject({ name: "AbortError" });
    caller.abort();

    await rejection;
    expect(receivedSignal?.aborted).toBe(true);
  });

  it("keeps the timeout active while the response body is being read", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn((_path: RequestInfo | URL, init?: RequestInit) => Promise.resolve({
      ok: true,
      json: () => new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(init.signal?.reason), { once: true });
      })
    } as Response));
    vi.stubGlobal("fetch", fetchMock);

    const request = getJson("/api/slow-body", { timeoutMs: 30_000 });
    const rejection = expect(request).rejects.toMatchObject({ name: "TimeoutError" });
    await vi.advanceTimersByTimeAsync(30_000);

    await rejection;
  });
});
