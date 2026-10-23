export function completeSum(values: Array<number | null | undefined>): number | null {
  return values.length && values.every(v => typeof v === "number" && Number.isFinite(v))
    ? values.reduce<number>((total, value) => total + (value as number), 0) : null;
}
export function observationState(value: string | undefined | null, now = Date.now(), maxAgeSeconds = 90): string {
  if (!value || !Number.isFinite(Date.parse(value))) return "UNAVAILABLE";
  const age = (now - Date.parse(value)) / 1000;
  return age < 0 ? "FUTURE_OBSERVATION" : age <= maxAgeSeconds ? "FRESH" : "STALE";
}
