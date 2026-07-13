import { describe, expect, it } from "vitest";
import { LruTtlCache } from "./pollingCache";

describe("LruTtlCache", () => {
  it("evicts the least recently used entry at its maximum size", () => {
    const cache = new LruTtlCache<number>(3, 1_000);
    cache.set("a", 1, 0);
    cache.set("b", 2, 0);
    cache.set("c", 3, 0);
    expect(cache.get("a", 1)).toBe(1);

    cache.set("d", 4, 2);

    expect(cache.size).toBe(3);
    expect(cache.get("b", 2)).toBeUndefined();
    expect(cache.keys()).toEqual(expect.arrayContaining(["a", "c", "d"]));
  });

  it("expires five-minute-style entries from insertion time without extending TTL on reads", () => {
    const cache = new LruTtlCache<string>(50, 300_000);
    cache.set("job", "value", 1_000);

    expect(cache.get("job", 200_000)).toBe("value");
    expect(cache.get("job", 301_000)).toBeUndefined();
    expect(cache.size).toBe(0);
  });
});
