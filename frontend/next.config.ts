import type { NextConfig } from "next";

/** The legacy /trades page is replaced by /tradebook. Next passes the request query string through to the destination. */
export const redirects = async () => [{ source: "/trades", destination: "/tradebook", permanent: false }];
// Only for worktrees whose node_modules is a symlink to another checkout: lets Turbopack resolve files outside this directory.
const turbopackRoot = process.env.AI_TRADE_TURBOPACK_ROOT;
const nextConfig: NextConfig = { reactStrictMode: true, output: "standalone", redirects, allowedDevOrigins: ["127.0.0.1"], ...(turbopackRoot ? { turbopack: { root: turbopackRoot } } : {}) };
export default nextConfig;
