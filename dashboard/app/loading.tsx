export default function Loading() {
  return (
    <div className="scandi-page" style={{ padding: "48px 24px", textAlign: "center", color: "var(--m3-sys-on-surface-variant)" }}>
      <div style={{ fontSize: 14, letterSpacing: "0.04em", textTransform: "uppercase", opacity: 0.7 }}>
        Loading trader state
      </div>
      <div style={{ marginTop: 24, display: "inline-block", width: 24, height: 24, borderRadius: "50%", border: "2px solid currentColor", borderRightColor: "transparent", animation: "spin 0.9s linear infinite" }} />
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
