import type { Metadata, Viewport } from "next";
import { Suspense } from "react";
import "./globals.css";
import "./design-system.css";
import "./monitoring.css";
import "./terminal.css";
import { Navigation } from "@/components/navigation";
import { GlobalHeader } from "@/components/global-header";
import { FiltersProvider } from "@/components/filters-context";
import { DrawerHost } from "@/components/pages/drawers";
import { LoadingState } from "@/components/terminal/states";

export const metadata: Metadata = {
  title: "AI Trade — Paper Trading Terminal",
  description: "Private, read-only paper-trading monitor for NIFTY and MCX strategies",
  manifest: "/manifest.webmanifest",
  icons: { icon: "/favicon.svg", apple: [{ url: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" }] },
  appleWebApp: { capable: true, title: "AI Trade", statusBarStyle: "black-translucent" },
};
export const viewport: Viewport = { width: "device-width", initialScale: 1, viewportFit: "cover", themeColor: [{ media: "(prefers-color-scheme: dark)", color: "#0a0d12" }, { media: "(prefers-color-scheme: light)", color: "#eef1f5" }], colorScheme: "dark light" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en" data-theme="dark" suppressHydrationWarning><head><script dangerouslySetInnerHTML={{ __html: "try{document.documentElement.dataset.theme=localStorage.getItem('ai-trade-theme')==='light'?'light':'dark'}catch{}" }}/></head>
    <body><Suspense fallback={<div style={{ padding: 16 }}><LoadingState label="Loading terminal"/></div>}>
      <FiltersProvider>
        <div className="tm-app">
          <Navigation/>
          <div className="tm-main">
            <GlobalHeader/>
            <div className="tm-content" id="workspace-content" tabIndex={-1}>{children}</div>
            <footer className="tm-footer"><span>AI Trade · PAPER trading only · all figures simulated</span><span>Read-only · no order, square-off or enable controls · times Asia/Kolkata</span></footer>
          </div>
        </div>
        <DrawerHost/>
      </FiltersProvider>
    </Suspense></body></html>;
}
