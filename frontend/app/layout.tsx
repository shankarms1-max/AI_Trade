import type { Metadata, Viewport } from "next";
import "./globals.css";
import "./design-system.css";
import { Navigation } from "@/components/navigation";

export const metadata: Metadata = {
  title: "AI Trade — Research Intelligence",
  description: "Private, read-only NIFTY and Commodity trading research analytics",
  manifest: "/manifest.webmanifest",
  icons: { icon: "/favicon.svg", apple: [{ url: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" }] },
  appleWebApp: { capable: true, title: "NIFTY Research", statusBarStyle: "black-translucent" },
};
export const viewport: Viewport = { width: "device-width", initialScale: 1, viewportFit: "cover", themeColor: [{ media: "(prefers-color-scheme: dark)", color: "#0c0c13" }, { media: "(prefers-color-scheme: light)", color: "#f5f6fa" }], colorScheme: "dark light" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en" data-theme="dark" suppressHydrationWarning><head><script dangerouslySetInnerHTML={{ __html: "try{document.documentElement.dataset.theme=localStorage.getItem('ai-trade-theme')==='light'?'light':'dark'}catch{}" }}/></head><body><Navigation/><div className="workspace-main"><div className="page-content" id="workspace-content">{children}</div><footer className="workspace-footer"><span>AI Trade <i/> Evidence before conviction.</span><span>Paper research only · No broker order controls</span></footer></div></body></html>;
}
