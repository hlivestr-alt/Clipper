// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useDebouncedValue } from "./useDebouncedValue";

afterEach(() => vi.useRealTimers());

describe("useDebouncedValue", () => {
  it("waits 300ms before exposing a new search value", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(({ value }) => useDebouncedValue(value, 300), {
      initialProps: { value: "" }
    });

    rerender({ value: "tea" });
    expect(result.current).toBe("");
    act(() => vi.advanceTimersByTime(299));
    expect(result.current).toBe("");
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe("tea");
  });
});
