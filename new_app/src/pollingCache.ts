export type CacheEntry<T> = {
  value: T;
  expiresAt: number;
  lastAccess: number;
};

export class LruTtlCache<T> {
  private readonly entries = new Map<string, CacheEntry<T>>();
  private accessSequence = 0;

  constructor(
    private readonly maxEntries: number,
    private readonly ttlMs: number
  ) {}

  get size(): number {
    return this.entries.size;
  }

  get(key: string, now = Date.now()): T | undefined {
    const entry = this.entries.get(key);
    if (!entry) {
      return undefined;
    }
    if (entry.expiresAt <= now) {
      this.entries.delete(key);
      return undefined;
    }
    entry.lastAccess = ++this.accessSequence;
    return entry.value;
  }

  set(key: string, value: T, now = Date.now()): void {
    this.deleteExpired(now);
    this.entries.set(key, {
      value,
      expiresAt: now + this.ttlMs,
      lastAccess: ++this.accessSequence
    });
    while (this.entries.size > this.maxEntries) {
      let oldestKey: string | undefined;
      let oldestAccess = Number.POSITIVE_INFINITY;
      for (const [candidateKey, entry] of this.entries) {
        if (entry.lastAccess < oldestAccess) {
          oldestKey = candidateKey;
          oldestAccess = entry.lastAccess;
        }
      }
      if (oldestKey === undefined) {
        break;
      }
      this.entries.delete(oldestKey);
    }
  }

  clear(): void {
    this.entries.clear();
    this.accessSequence = 0;
  }

  keys(): string[] {
    return Array.from(this.entries.keys());
  }

  private deleteExpired(now: number): void {
    for (const [key, entry] of this.entries) {
      if (entry.expiresAt <= now) {
        this.entries.delete(key);
      }
    }
  }
}
