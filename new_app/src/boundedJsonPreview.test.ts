import { describe, expect, it } from "vitest";
import { boundedJsonPreview } from "./boundedJsonPreview";

describe("boundedJsonPreview", () => {
  it("enforces the global output limit without using toJSON", () => {
    const value = {
      toJSON: () => {
        throw new Error("the original object must not be stringified");
      },
      rows: Array.from({ length: 100 }, (_, index) => ({
        index,
        text: "x".repeat(2_000)
      }))
    };

    const preview = boundedJsonPreview(value);

    expect(preview.text.length).toBeLessThanOrEqual(20_000);
    expect(preview.truncated).toBe(true);
    expect(preview.text).toContain('"rows"');
  });

  it("limits object keys, arrays, strings, and depth before reading omitted data", () => {
    const value: Record<string, unknown> = {};
    for (let index = 0; index < 50; index += 1) {
      value[`key_${index}`] = index;
    }
    Object.defineProperty(value, "must_not_be_read", {
      enumerable: true,
      get: () => {
        throw new Error("omitted property was read");
      }
    });
    value.first = {
      second: {
        third: {
          fourth: {
            fifth: "too deep"
          }
        }
      }
    };

    const array = Array.from({ length: 101 }, (_, index) => index);
    Object.defineProperty(array, 100, {
      enumerable: true,
      get: () => {
        throw new Error("omitted array item was read");
      }
    });

    const preview = boundedJsonPreview({
      value,
      array,
      long: "z".repeat(2_100),
      deep: { one: { two: { three: { four: "too deep" } } } }
    });

    expect(preview.truncated).toBe(true);
    expect(preview.text).toContain("additional keys omitted");
    expect(preview.text).toContain("additional items omitted");
    expect(preview.text).toContain("string truncated");
    expect(preview.text).toContain("maximum depth reached");
  });

  it("replaces circular references safely", () => {
    const value: Record<string, unknown> = { name: "job" };
    value.self = value;

    const preview = boundedJsonPreview(value);

    expect(preview.circular).toBe(true);
    expect(preview.truncated).toBe(true);
    expect(preview.text).toContain("[circular]");
  });

  it("limits deeply nested values and oversized property names", () => {
    const preview = boundedJsonPreview({
      ["k".repeat(2_100)]: { one: { two: { three: { four: "hidden" } } } }
    });

    expect(preview.truncated).toBe(true);
    expect(preview.text).toContain("string truncated");
    expect(preview.text).toContain("maximum depth reached");
    expect(preview.text.length).toBeLessThanOrEqual(20_000);
  });

  it("does not mark exact key, item, and string boundaries as truncated", () => {
    const value = Object.fromEntries(
      Array.from({ length: 50 }, (_, index) => [`key_${index}`, index])
    );
    const preview = boundedJsonPreview({
      value,
      items: Array.from({ length: 100 }, (_, index) => index),
      text: "x".repeat(2_000)
    });

    expect(preview.truncated).toBe(false);
    expect(preview.text.length).toBeLessThanOrEqual(20_000);
  });
});
