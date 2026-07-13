export type JsonPreview = {
  text: string;
  truncated: boolean;
  circular: boolean;
};

export type JsonPreviewLimits = {
  maxChars: number;
  maxDepth: number;
  maxObjectKeys: number;
  maxArrayItems: number;
  maxStringChars: number;
};

export const DEFAULT_JSON_PREVIEW_LIMITS: JsonPreviewLimits = {
  maxChars: 20_000,
  maxDepth: 4,
  maxObjectKeys: 50,
  maxArrayItems: 100,
  maxStringChars: 2_000
};

type PreviewState = {
  chunks: string[];
  length: number;
  truncated: boolean;
  circular: boolean;
  ancestors: WeakSet<object>;
};

function quoted(value: string): string {
  return JSON.stringify(value);
}

function boundedString(value: string, state: PreviewState, limits: JsonPreviewLimits): string {
  if (value.length <= limits.maxStringChars) {
    return value;
  }
  state.truncated = true;
  return `${value.slice(0, limits.maxStringChars)}\u2026 [string truncated]`;
}

function boundedQuoted(value: string, state: PreviewState, limits: JsonPreviewLimits): string {
  return quoted(boundedString(value, state, limits));
}

function append(state: PreviewState, value: string, maxChars: number): boolean {
  const remaining = maxChars - state.length;
  if (remaining <= 0) {
    state.truncated = true;
    return false;
  }
  if (value.length > remaining) {
    state.chunks.push(value.slice(0, remaining));
    state.length = maxChars;
    state.truncated = true;
    return false;
  }
  state.chunks.push(value);
  state.length += value.length;
  return true;
}

function renderString(state: PreviewState, value: string, limits: JsonPreviewLimits): void {
  append(state, boundedQuoted(value, state, limits), limits.maxChars);
}

function renderValue(
  state: PreviewState,
  value: unknown,
  depth: number,
  limits: JsonPreviewLimits
): void {
  if (state.length >= limits.maxChars) {
    state.truncated = true;
    return;
  }
  if (value === null) {
    append(state, "null", limits.maxChars);
    return;
  }
  if (typeof value === "string") {
    renderString(state, value, limits);
    return;
  }
  if (typeof value === "number") {
    append(state, Number.isFinite(value) ? String(value) : quoted(String(value)), limits.maxChars);
    return;
  }
  if (typeof value === "boolean") {
    append(state, String(value), limits.maxChars);
    return;
  }
  if (typeof value === "undefined") {
    append(state, quoted("[undefined]"), limits.maxChars);
    return;
  }
  if (typeof value === "bigint") {
    append(state, quoted(`${value.toString()}n`), limits.maxChars);
    return;
  }
  if (typeof value === "symbol" || typeof value === "function") {
    append(state, quoted(`[${typeof value}]`), limits.maxChars);
    return;
  }

  if (state.ancestors.has(value)) {
    state.circular = true;
    state.truncated = true;
    append(state, quoted("[circular]"), limits.maxChars);
    return;
  }
  if (depth >= limits.maxDepth) {
    state.truncated = true;
    append(state, quoted("[maximum depth reached]"), limits.maxChars);
    return;
  }

  state.ancestors.add(value);
  try {
    if (Array.isArray(value)) {
      append(state, "[", limits.maxChars);
      const itemCount = Math.min(value.length, limits.maxArrayItems);
      for (let index = 0; index < itemCount && state.length < limits.maxChars; index += 1) {
        append(state, `${index === 0 ? "" : ","}\n${"  ".repeat(depth + 1)}`, limits.maxChars);
        renderValue(state, value[index], depth + 1, limits);
      }
      if (value.length > limits.maxArrayItems && state.length < limits.maxChars) {
        state.truncated = true;
        append(state, `${itemCount === 0 ? "" : ","}\n${"  ".repeat(depth + 1)}`, limits.maxChars);
        renderString(state, `[${value.length - limits.maxArrayItems} additional items omitted]`, limits);
      }
      if (itemCount > 0 || value.length > limits.maxArrayItems) {
        append(state, `\n${"  ".repeat(depth)}`, limits.maxChars);
      }
      append(state, "]", limits.maxChars);
      return;
    }

    append(state, "{", limits.maxChars);
    let renderedKeys = 0;
    let hasAdditionalKeys = false;
    for (const key in value as Record<string, unknown>) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) {
        continue;
      }
      if (renderedKeys >= limits.maxObjectKeys) {
        hasAdditionalKeys = true;
        break;
      }
      append(state, `${renderedKeys === 0 ? "" : ","}\n${"  ".repeat(depth + 1)}${boundedQuoted(key, state, limits)}: `, limits.maxChars);
      try {
        renderValue(state, (value as Record<string, unknown>)[key], depth + 1, limits);
      } catch (error) {
        state.truncated = true;
        renderString(state, `[property could not be read: ${error instanceof Error ? error.message : String(error)}]`, limits);
      }
      renderedKeys += 1;
      if (state.length >= limits.maxChars) {
        break;
      }
    }
    if (hasAdditionalKeys && state.length < limits.maxChars) {
      state.truncated = true;
      append(state, `${renderedKeys === 0 ? "" : ","}\n${"  ".repeat(depth + 1)}${quoted("__preview__")}: `, limits.maxChars);
      renderString(state, "[additional keys omitted]", limits);
      renderedKeys += 1;
    }
    if (renderedKeys > 0) {
      append(state, `\n${"  ".repeat(depth)}`, limits.maxChars);
    }
    append(state, "}", limits.maxChars);
  } finally {
    state.ancestors.delete(value);
  }
}

export function boundedJsonPreview(
  value: unknown,
  overrides: Partial<JsonPreviewLimits> = {}
): JsonPreview {
  const limits = { ...DEFAULT_JSON_PREVIEW_LIMITS, ...overrides };
  const state: PreviewState = {
    chunks: [],
    length: 0,
    truncated: false,
    circular: false,
    ancestors: new WeakSet<object>()
  };
  renderValue(state, value, 0, limits);
  return {
    text: state.chunks.join("") || "-",
    truncated: state.truncated,
    circular: state.circular
  };
}
