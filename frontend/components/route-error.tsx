"use client";

/** Generic, retryable route error: never echoes the error message (it may carry server details). */
export function RouteError({ title, retry, reset }: { title: string; retry?: () => void; reset?: () => void }) {
  return <main className="terminal-error" role="alert">
    <strong>{title} UNAVAILABLE</strong>
    <span>This page could not render. Other pages keep working. No trading action was taken.</span>
    <button type="button" onClick={() => (retry ?? reset)?.()}>Retry page</button>
  </main>;
}
