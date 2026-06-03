import "./_m3/tokens.css";
import "./_m3/components.css";
import "./_m3/overlay.css";
import "./globals.css";
import "./_m3/scandi.css";
import "./_m3/trader-accents.css";

export const metadata = {
  title: "Trader",
  description: "Dip-buy bot — IBKR",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" data-app-mode="scandi" style={{ ["--m3-source-hue" as string]: "150" }}>
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Inter:wght@200;300;400;450;500;600&family=JetBrains+Mono:wght@300;400;500&display=swap"
        />
      </head>
      <body className="scandi scandi-active" data-app="trader">
        <div className="scandi-page">{children}</div>
      </body>
    </html>
  );
}
