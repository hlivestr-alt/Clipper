import { useEffect, useSyncExternalStore } from "react";
import { invalidateApiPrefix } from "./queryClient";

export type LiveUpdateMode = "connecting" | "live" | "polling";

export type LiveUpdateSnapshot = {
  mode: LiveUpdateMode;
  connected: boolean;
  lastEventAt?: number;
  reconnects: number;
};

type InvalidationEvent = {
  schema_version: number;
  topics?: string[];
  revisions?: Record<string, number>;
  occurred_at?: string;
};

const topicPrefixes: Record<string, string[]> = {
  queue: ["/api/dashboard", "/api/queue", "/api/overview"],
  jobs: ["/api/control/jobs", "/api/overview"],
  scores: ["/api/scores", "/api/overview"],
  compliance: ["/api/compliance", "/api/overview"],
  modules: ["/api/modules"],
  outputs: ["/api/scores", "/api/compliance", "/api/overview"],
  settings: ["/api/settings", "/api/system", "/api/dashboard", "/api/queue"],
  variations: ["/api/variations"],
  system: ["/api/system"],
  logs: ["/api/logs"]
};

let snapshot: LiveUpdateSnapshot = { mode: "connecting", connected: false, reconnects: 0 };
const listeners = new Set<() => void>();
let source: EventSource | undefined;
let users = 0;
let connectTimer: ReturnType<typeof setTimeout> | undefined;

function update(next: Partial<LiveUpdateSnapshot>): void {
  snapshot = { ...snapshot, ...next };
  listeners.forEach((listener) => listener());
}

export function getLiveUpdateSnapshot(): LiveUpdateSnapshot {
  return snapshot;
}

export function subscribeLiveUpdates(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function invalidationPrefixesForTopics(topics: string[]): string[] {
  const prefixes = new Set<string>();
  if (topics.includes("*")) {
    Object.values(topicPrefixes).flat().forEach((prefix) => prefixes.add(prefix));
  } else {
    topics.forEach((topic) => topicPrefixes[topic]?.forEach((prefix) => prefixes.add(prefix)));
  }
  return Array.from(prefixes);
}

function invalidateTopics(topics: string[]): void {
  invalidationPrefixesForTopics(topics).forEach((prefix) => void invalidateApiPrefix(prefix));
}

export function processLiveInvalidation(raw: string, reset = false): void {
  try {
    const payload = JSON.parse(raw) as InvalidationEvent;
    invalidateTopics(reset ? ["*"] : payload.topics ?? []);
    update({ lastEventAt: Date.now() });
  } catch {
    invalidateTopics(["*"]);
  }
}

function start(): void {
  if (source || typeof EventSource === "undefined") {
    if (typeof EventSource === "undefined") {
      update({ mode: "polling", connected: false });
    }
    return;
  }
  update({ mode: "connecting", connected: false });
  source = new EventSource("/api/events");
  connectTimer = setTimeout(() => {
    if (!snapshot.connected) {
      update({ mode: "polling", connected: false });
    }
  }, 10_000);
  source.onopen = () => {
    if (connectTimer) clearTimeout(connectTimer);
    update({ mode: "live", connected: true });
    invalidateTopics(["*"]);
  };
  source.addEventListener("invalidate", (event) => {
    processLiveInvalidation((event as MessageEvent<string>).data);
  });
  source.addEventListener("reset", (event) => {
    processLiveInvalidation((event as MessageEvent<string>).data, true);
  });
  source.onerror = () => {
    update({ mode: "polling", connected: false, reconnects: snapshot.reconnects + 1 });
  };
}

function retain(): () => void {
  users += 1;
  start();
  return () => {
    users = Math.max(0, users - 1);
    if (users === 0) {
      if (connectTimer) clearTimeout(connectTimer);
      source?.close();
      source = undefined;
    }
  };
}

export function useLiveUpdateStatus(): LiveUpdateSnapshot {
  const current = useSyncExternalStore(subscribeLiveUpdates, getLiveUpdateSnapshot, getLiveUpdateSnapshot);
  useEffect(() => retain(), []);
  return current;
}

export function shouldPollWhileLive(path: string): boolean {
  return ["/api/health", "/api/system", "/api/logs", "/api/queue/vods"].some((prefix) =>
    path.startsWith(prefix)
  );
}

export function resetLiveUpdatesForTests(): void {
  if (connectTimer) clearTimeout(connectTimer);
  source?.close();
  source = undefined;
  users = 0;
  snapshot = { mode: "connecting", connected: false, reconnects: 0 };
  listeners.clear();
}
