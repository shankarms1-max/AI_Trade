"use client";
import { ChargeModel } from "@/components/pages/system-sections";
import { ResourceView } from "@/components/terminal/states";
import { KV, PageTitle, Panel, StatusBadge } from "@/components/terminal/primitives";
import { ThemeToggle } from "@/components/theme-toggle";
import { ANALYTICS_INTERVAL, LIVE_INTERVAL, STATIC_INTERVAL, useResource } from "@/hooks/use-resource";
import { API_BASE } from "@/lib/http";
import { parse, urls } from "@/lib/portfolio-api";
import pkg from "@/package.json";

/** Read-only: the only control on this page is the theme toggle. Everything else is displayed, nothing is configurable. */
export function Settings() {
  const sources = useResource(urls.sources(), parse.sources, ANALYTICS_INTERVAL);
  const build = process.env.NEXT_PUBLIC_BUILD_ID;
  return <div>
    <PageTitle title="Settings" note="Read-only. This terminal has no trading controls; these are display facts."/>
    <div className="tm-grid c2">
      <Panel title="Display"><KV items={[
        ["Theme", <span key="t" style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><ThemeToggle/><span className="tm-muted">dark is the default; stored in this browser only</span></span>],
        ["Displayed timezone", <span key="z">Asia/Kolkata (IST). Naive timestamps from the API are UTC.</span>],
        ["Session grouping", "IST session date"],
      ]}/></Panel>
      <Panel title="Mode and polling"><KV items={[
        ["Default mode", <StatusBadge key="m" value="PAPER" tone="info"/>],
        ["Live execution", <span key="l">None. LIVE totals are never mixed with PAPER.</span>],
        ["Polling: live pages", `${LIVE_INTERVAL / 1000} s (one shared request per URL; paused while the tab is hidden)`],
        ["Polling: analytics", `${ANALYTICS_INTERVAL / 1000} s`], ["Polling: reference data", `${STATIC_INTERVAL / 1000} s`],
        ["Failure behaviour", "Exponential back-off to 60 s; last good data stays on screen, marked STALE"],
      ]}/></Panel>
    </div>
    <ResourceView resource={sources} label="Charge model" skeletonRows={2}>{d => <ChargeModel sources={d}/>}</ResourceView>
    <Panel title="Build"><KV items={[
      ["Frontend", `${pkg.name} ${pkg.version}`], ["Build id", build ?? "not provided"],
      ["API base", API_BASE === "" ? "same-origin" : API_BASE], ["Backend version", "not exposed by the API"],
    ]}/></Panel>
  </div>;
}
