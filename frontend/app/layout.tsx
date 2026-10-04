import type { Metadata } from "next";
import "./globals.css";
import { Navigation } from "@/components/navigation";

export const metadata: Metadata = { title: "NIFTY Research Terminal", description: "Read-only NIFTY credit-spread research terminal", icons: { icon: "/favicon.svg" } };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="en"><body><div className="shell"><Navigation />{children}<div className="footer-note">RESEARCH ONLY · NO EXECUTION CONTROLS</div></div></body></html>; }
