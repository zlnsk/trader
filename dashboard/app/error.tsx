"use client";

import { useEffect } from "react";

export default function Error({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error("Trader dashboard error", error);
  }, [error]);
  return (
    <div className="scandi-page" style={{ padding: "48px 24px", maxWidth: 640, margin: "0 auto", color: "var(--m3-sys-on-surface)" }}>
      <div style={{ fontSize: 12, letterSpacing: "0.08em", textTransform: "uppercase", opacity: 0.6 }}>Error</div>
      <h1 style={{ marginTop: 8, fontSize: 28, fontWeight: 500, letterSpacing: "-0.02em" }}>Could not load trader state</h1>
      <p style={{ marginTop: 12, opacity: 0.75, fontSize: 15, lineHeight: 1.55 }}>
        The dashboard failed to compute its initial state. IB Gateway may be disconnected or the database is unreachable.
      </p>
      {error.digest && (
        <div style={{ marginTop: 16, fontFamily: "var(--font-mono, monospace)", fontSize: 12, opacity: 0.5 }}>digest: {error.digest}</div>
      )}
      <button
        onClick={reset}
        style={{
          marginTop: 24,
          padding: "10px 18px",
          border: "1px solid var(--m3-sys-outline-variant)",
          borderRadius: 999,
          background: "transparent",
          color: "inherit",
          font: "inherit",
          cursor: "pointer",
        }}
      >
        Try again
      </button>
    </div>
  );
}
