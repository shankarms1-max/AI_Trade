"use client";

/** `retry` re-fetches and re-renders the segment (preferred in this Next version); `reset` is the fallback. */
export default function Error({ retry, reset }: { error: Error & { digest?: string }; retry?: () => void; reset?: () => void }) {
  return <main className="terminal-error" role="alert">
    <strong>PAGE UNAVAILABLE</strong>
    <span>This page could not load. No trading action was taken.</span>
    <button type="button" onClick={() => (retry ?? reset)?.()}>Retry page</button>
  </main>;
}
