import type { ReactNode } from "react";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

export type AppearancePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const APPEARANCE_STORAGE_KEY = "clipper.appearance";
export const DEFAULT_APPEARANCE: AppearancePreference = "dark";

const appearanceValues: AppearancePreference[] = ["system", "light", "dark"];

export function isAppearancePreference(value: string | null | undefined): value is AppearancePreference {
  return Boolean(value && appearanceValues.includes(value as AppearancePreference));
}

export function readAppearancePreference(storage?: Pick<Storage, "getItem"> | null): AppearancePreference {
  try {
    const source = storage ?? (typeof window !== "undefined" ? window.localStorage : null);
    const stored = source?.getItem(APPEARANCE_STORAGE_KEY);
    return isAppearancePreference(stored) ? stored : DEFAULT_APPEARANCE;
  } catch {
    return DEFAULT_APPEARANCE;
  }
}

export function writeAppearancePreference(
  preference: AppearancePreference,
  storage?: Pick<Storage, "setItem"> | null
): void {
  try {
    const target = storage ?? (typeof window !== "undefined" ? window.localStorage : null);
    target?.setItem(APPEARANCE_STORAGE_KEY, preference);
  } catch {
    // The in-memory preference still applies when browser storage is unavailable.
  }
}

export function resolveTheme(preference: AppearancePreference, systemTheme: ResolvedTheme): ResolvedTheme {
  return preference === "system" ? systemTheme : preference;
}

export function readSystemTheme(): ResolvedTheme {
  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return "dark";
}

export function applyTheme(preference: AppearancePreference, systemTheme = readSystemTheme()): ResolvedTheme {
  const resolvedTheme = resolveTheme(preference, systemTheme);
  if (typeof document !== "undefined") {
    document.documentElement.dataset.appearance = preference;
    document.documentElement.dataset.theme = resolvedTheme;
  }
  return resolvedTheme;
}

export function initializeAppearanceTheme(): void {
  applyTheme(readAppearancePreference());
}

type AppearanceContextValue = {
  appearance: AppearancePreference;
  resolvedTheme: ResolvedTheme;
  setAppearance: (preference: AppearancePreference) => void;
};

const AppearanceContext = createContext<AppearanceContextValue | null>(null);

export function AppearanceProvider({ children }: { children: ReactNode }) {
  const [appearance, setAppearanceState] = useState<AppearancePreference>(() => readAppearancePreference());
  const [systemTheme, setSystemTheme] = useState<ResolvedTheme>(() => readSystemTheme());

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const sync = (event?: MediaQueryListEvent) => {
      setSystemTheme(event ? (event.matches ? "dark" : "light") : (media.matches ? "dark" : "light"));
    };
    sync();
    if (media.addEventListener) {
      media.addEventListener("change", sync);
    } else {
      media.addListener?.(sync);
    }
    return () => {
      if (media.removeEventListener) {
        media.removeEventListener("change", sync);
      } else {
        media.removeListener?.(sync);
      }
    };
  }, []);

  const resolvedTheme = resolveTheme(appearance, systemTheme);

  useEffect(() => {
    applyTheme(appearance, systemTheme);
  }, [appearance, systemTheme]);

  const setAppearance = useCallback((next: AppearancePreference) => {
    setAppearanceState(next);
    writeAppearancePreference(next);
  }, []);

  const value = useMemo(
    () => ({ appearance, resolvedTheme, setAppearance }),
    [appearance, resolvedTheme, setAppearance]
  );

  return <AppearanceContext.Provider value={value}>{children}</AppearanceContext.Provider>;
}

export function useAppearance(): AppearanceContextValue {
  const context = useContext(AppearanceContext);
  if (!context) {
    throw new Error("useAppearance must be used within AppearanceProvider");
  }
  return context;
}
