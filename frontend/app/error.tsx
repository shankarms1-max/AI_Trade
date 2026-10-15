"use client";

export default function Error({ retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return <main className="terminal-error" role="alert">
    <strong>PAGE UNAVAILABLE</strong>
    <span>This page could not load. No trading action was taken.</span>
    <button type="button" onClick={retry}>Retry page</button>
  </main>;
}
