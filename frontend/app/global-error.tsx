"use client";

export default function GlobalError({ retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return <html lang="en"><body style={{ margin: 0, background: "#0c1118", color: "#e1e8ef", fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace" }}>
    <main role="alert" style={{ maxWidth: 520, margin: "12vh auto", padding: 24, border: "1px solid #3d5363", background: "#121c26" }}>
      <strong style={{ display: "block", marginBottom: 12, color: "#f3bd69" }}>TERMINAL UNAVAILABLE</strong>
      <p style={{ fontSize: 13, lineHeight: 1.5 }}>The application could not load. No trading action was taken.</p>
      <button type="button" onClick={retry} style={{ marginTop: 12, padding: "8px 12px", border: "1px solid #5d7889", background: "#1c2e3b", color: "#e1e8ef", cursor: "pointer" }}>Retry application</button>
    </main>
  </body></html>;
}
