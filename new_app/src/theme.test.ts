import { describe, expect, it } from "vitest";
import {
  APPEARANCE_STORAGE_KEY,
  DEFAULT_APPEARANCE,
  readAppearancePreference,
  resolveTheme,
  writeAppearancePreference
} from "./theme";

function storage(values: Record<string, string> = {}) {
  const data = new Map(Object.entries(values));
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => data.set(key, value),
    value: (key: string) => data.get(key)
  };
}

describe("appearance preference", () => {
  it("keeps dark as the default when no saved preference exists", () => {
    expect(readAppearancePreference(storage())).toBe(DEFAULT_APPEARANCE);
    expect(readAppearancePreference(storage({ [APPEARANCE_STORAGE_KEY]: "invalid" }))).toBe("dark");
  });

  it("reads and persists the supported appearance values", () => {
    const saved = storage({ [APPEARANCE_STORAGE_KEY]: "light" });
    expect(readAppearancePreference(saved)).toBe("light");
    writeAppearancePreference("system", saved);
    expect(saved.value(APPEARANCE_STORAGE_KEY)).toBe("system");
  });

  it("resolves System to the current operating system theme", () => {
    expect(resolveTheme("system", "light")).toBe("light");
    expect(resolveTheme("system", "dark")).toBe("dark");
    expect(resolveTheme("light", "dark")).toBe("light");
    expect(resolveTheme("dark", "light")).toBe("dark");
  });
});
