"use client";
import type { ReactNode } from "react";
import type { PollingState } from "@/hooks/use-polling";
import { describeFailure } from "@/lib/http";
import { timeIST } from "@/lib/format";
import { Icon } from "@/components/icons";

type Poll = Pick<PollingState<unknown>, "data" | "failure" | "staleSince" | "nextAttemptAt" | "consecutiveFailures" | "refresh">;
const at = (ms: number | null) => ms == null ? "—" : timeIST(new Date(ms).toISOString());

export function RetryButton({ onRetry, label = "Retry now" }: { onRetry: () => void; label?: string }) {
  return <button type="button" className="poll-retry" onClick={onRetry}><Icon name="refresh" size={14}/>{label}</button>;
}

/**
 * Read-status line for a polled source. Shows nothing while healthy; a "stale since" line (keeping
 * the last good data on screen) when a refresh fails; a typed error block when nothing was ever loaded.
 */
export function PollStatus({ poll, label, notConfigured }: { poll: Poll; label: string; notConfigured?: ReactNode }) {
  const { failure } = poll;
  if (!failure) return null;
  const retrying = poll.nextAttemptAt ? ` · next attempt ${at(poll.nextAttemptAt)}` : "";
  const failures = poll.consecutiveFailures > 1 ? ` · ${poll.consecutiveFailures} consecutive failures` : "";
  if (failure.kind === "not_configured" && notConfigured) {
    return <div className="poll-status is-not-configured" role="status" data-kind={failure.kind}>{notConfigured}</div>;
  }
  if (poll.data != null) {
    return <p className="poll-status is-stale" role="status" data-kind={failure.kind}>
      <strong>STALE since {at(poll.staleSince)}</strong>
      <span>{label}: {describeFailure(failure)}{failures}{retrying}. Showing the last successful read.</span>
      <RetryButton onRetry={() => void poll.refresh()}/>
    </p>;
  }
  return <div className="section-error poll-status is-failed" role="alert" data-kind={failure.kind}>
    <strong>{label} UNAVAILABLE</strong>
    <span>{describeFailure(failure)}{failures}{retrying}</span>
    <RetryButton onRetry={() => void poll.refresh()}/>
  </div>;
}
