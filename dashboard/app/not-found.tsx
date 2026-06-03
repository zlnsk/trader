export default function NotFound() {
  return (
    <div className="scandi-page" style={{ padding: "48px 24px", maxWidth: 640, margin: "0 auto", textAlign: "center" }}>
      <div style={{ fontSize: 12, letterSpacing: "0.08em", textTransform: "uppercase", opacity: 0.6 }}>404</div>
      <h1 style={{ marginTop: 8, fontSize: 28, fontWeight: 500, letterSpacing: "-0.02em" }}>Not found</h1>
      <p style={{ marginTop: 12, opacity: 0.7 }}>This route is not part of the trader dashboard.</p>
    </div>
  );
}
