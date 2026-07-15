// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  invalidationPrefixesForTopics,
  resetLiveUpdatesForTests,
  shouldPollWhileLive,
  useLiveUpdateStatus
} from "./liveUpdates";

class FakeEventSource {
  static latest?: FakeEventSource;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (event: MessageEvent<string>) => void>();
  closed = false;

  constructor(public url: string) {
    FakeEventSource.latest = this;
  }

  addEventListener(name: string, listener: EventListener): void {
    this.listeners.set(name, listener as (event: MessageEvent<string>) => void);
  }

  close(): void {
    this.closed = true;
  }
}

afterEach(() => {
  resetLiveUpdatesForTests();
  delete (globalThis as { EventSource?: typeof EventSource }).EventSource;
});

describe("live invalidation", () => {
  it("maps topics to bounded query prefixes", () => {
    expect(invalidationPrefixesForTopics(["queue"])).toEqual([
      "/api/dashboard",
      "/api/queue",
      "/api/overview"
    ]);
    expect(invalidationPrefixesForTopics(["unknown"])).toEqual([]);
    expect(invalidationPrefixesForTopics(["*"])).toContain("/api/control/jobs");
  });

  it("keeps health, system, logs, and VOD sampling active while live", () => {
    expect(shouldPollWhileLive("/api/system")).toBe(true);
    expect(shouldPollWhileLive("/api/logs?lines=200")).toBe(true);
    expect(shouldPollWhileLive("/api/scores")).toBe(false);
  });

  it("moves from connecting to live and back to polling fallback", () => {
    (globalThis as { EventSource?: typeof EventSource }).EventSource = FakeEventSource as unknown as typeof EventSource;
    const hook = renderHook(() => useLiveUpdateStatus());
    expect(hook.result.current.mode).toBe("connecting");

    act(() => FakeEventSource.latest?.onopen?.());
    expect(hook.result.current.mode).toBe("live");
    expect(hook.result.current.connected).toBe(true);

    act(() => FakeEventSource.latest?.onerror?.());
    expect(hook.result.current.mode).toBe("polling");
    expect(hook.result.current.reconnects).toBe(1);
    hook.unmount();
    expect(FakeEventSource.latest?.closed).toBe(true);
  });
});
