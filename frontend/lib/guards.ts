import { PayloadError } from "@/lib/http";
import { parseTimestamp } from "@/lib/format";

/**
 * Minimal runtime guards for backend contracts. A field that violates its contract rejects the
 * whole payload with a PayloadError (kind "invalid_payload") so the UI shows a typed error state
 * instead of rendering guessed values. Nullable fields accept a missing key as null.
 */
export type Guard<T> = (value: unknown, path: string) => T;

export const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export function object(value: unknown, path: string): Record<string, unknown> {
  if (!isRecord(value)) throw new PayloadError(path, "expected object");
  return value;
}
export const string: Guard<string> = (value, path) => {
  if (typeof value !== "string") throw new PayloadError(path, "expected string");
  return value;
};
export const finite: Guard<number> = (value, path) => {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new PayloadError(path, "expected finite number");
  return value;
};
export const boolean: Guard<boolean> = (value, path) => {
  if (typeof value !== "boolean") throw new PayloadError(path, "expected boolean");
  return value;
};
export const timestamp: Guard<string> = (value, path) => {
  if (parseTimestamp(string(value, path)) == null) throw new PayloadError(path, "expected ISO timestamp");
  return value as string;
};
export function oneOf<T extends string>(allowed: readonly T[]): Guard<T> {
  return (value, path) => {
    if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) throw new PayloadError(path, `expected one of ${allowed.join("|")}`);
    return value as T;
  };
}
export function nullable<T>(guard: Guard<T>): Guard<T | null> {
  return (value, path) => value == null ? null : guard(value, path);
}
export function array<T>(guard: Guard<T>): Guard<T[]> {
  return (value, path) => {
    if (!Array.isArray(value)) throw new PayloadError(path, "expected array");
    return value.map((item, index) => guard(item, `${path}[${index}]`));
  };
}
export function record<T>(guard: Guard<T>): Guard<Record<string, T>> {
  return (value, path) => Object.fromEntries(Object.entries(object(value, path)).map(([key, item]) => [key, guard(item, `${path}.${key}`)]));
}
/** Validate a fixed shape; unknown extra keys are ignored (forward compatible). */
export function shape<T>(fields: { [K in keyof T]: Guard<T[K]> }): Guard<T> {
  return (value, path) => {
    const source = object(value, path);
    const result = {} as T;
    for (const key of Object.keys(fields) as Array<keyof T>) {
      result[key] = fields[key](source[key as string], path ? `${path}.${String(key)}` : String(key));
    }
    return result;
  };
}
export const nString = nullable(string);
export const nFinite = nullable(finite);
export const nTimestamp = nullable(timestamp);
