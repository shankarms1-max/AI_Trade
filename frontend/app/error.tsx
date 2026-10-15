"use client";

export default function Error({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return <main className="terminal-error" role="alert">
    <strong>PAGE UNAVAILABLE</strong>
    <span>This page could not load. No trading action was taken.</span>
    <button type="button" onClick={reset}>Retry page</button>
  </main>;
}
