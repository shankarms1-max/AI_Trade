"use client";
import type { ReactNode } from "react";
import { describeFailure, type RequestError } from "@/lib/http";
import { istClock } from "@/lib/terminal-format";
import type { Resource } from "@/hooks/use-resource";

export function LoadingState({ label = "Loading", rows = 4 }: { label?: string; rows?: number }) {
  return <div className="tm-state" role="status" aria-live="polite" aria-busy="true"><span className="sr-only">{label}</span>
    {Array.from({ length: rows }, (_, i) => <div key={i} className="tm-skeleton" style={{ width: `${92 - i * 11}%` }}/>)}</div>;
}
export function EmptyState({ children = "No records", hint }: { children?: ReactNode; hint?: ReactNode }) {
  return <div className="tm-state" role="status"><strong>{children}</strong>{hint ? <span>{hint}</span> : null}</div>;
}
const isDisconnected = (f: RequestError) => f.kind === "network" || f.kind === "timeout";
export function ErrorState({ failure, label, onRetry }: { failure: RequestError; label: string; onRetry?: () => void }) {
  const disconnected = isDisconnected(failure);
  return <div className="tm-state" data-tone={disconnected ? "warn" : "neg"} role="alert" data-kind={failure.kind}>
    <strong>{disconnected ? `DISCONNECTED · ${label}` : failure.kind === "not_configured" ? `NOT CONFIGURED · ${label}` : `${label} UNAVAILABLE`}</strong>
    <span>{describeFailure(failure)}{disconnected ? " · the API could not be reached; no data is shown rather than guessed values." : ""}</span>
    {onRetry && <button type="button" className="tm-btn" onClick={onRetry}>Retry</button>}
  </div>;
}
/** Shown while the last good data stays on screen after a failed refresh. */
export function StaleBar({ resource, label }: { resource: Pick<Resource<unknown>, "failure" | "staleSince" | "nextAttemptAt" | "consecutiveFailures" | "refresh">; label?: string }) {
  if (!resource.failure || resource.staleSince == null) return null;
  return <div className="tm-stale" role="status" data-kind={resource.failure.kind}>
    <b>STALE since {istClock(new Date(resource.staleSince).toISOString())} IST</b>
    <span>{label ? `${label}: ` : ""}{describeFailure(resource.failure)}{resource.consecutiveFailures > 1 ? ` · ${resource.consecutiveFailures} failures` : ""}{resource.nextAttemptAt ? ` · retrying ${istClock(new Date(resource.nextAttemptAt).toISOString())}` : ""} · showing last successful read</span>
    <button type="button" className="tm-btn" onClick={() => void resource.refresh()}>Retry</button>
  </div>;
}

/**
 * Standard data gate: Loading -> Error/Disconnected (with Retry) -> [stale bar +] content. Previous data is never discarded
 * on a failed refresh, so tables keep their scroll position, filters and selection.
 */
export function ResourceView<T>({ resource, label, children, isEmpty, empty, skeletonRows }: {
  resource: Resource<T>; label: string; children: (data: T) => ReactNode; isEmpty?: (data: T) => boolean; empty?: ReactNode; skeletonRows?: number;
}) {
  if (resource.data == null) {
    if (resource.failure) return <ErrorState failure={resource.failure} label={label} onRetry={() => void resource.refresh()}/>;
    return <LoadingState label={`Loading ${label}`} rows={skeletonRows}/>;
  }
  return <><StaleBar resource={resource} label={label}/>{isEmpty?.(resource.data) ? (empty ?? <EmptyState>No records</EmptyState>) : children(resource.data)}</>;
}
