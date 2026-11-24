"use client";
import { defaultRangeFor, useUrlState } from "@/components/filters-context";
import { PerformanceReport } from "@/components/pages/performance-report";
import { DateRangeSelector } from "@/components/terminal/filters";
import { LiveGate, ResourceView } from "@/components/terminal/states";
import { PageTitle } from "@/components/terminal/primitives";
import { ANALYTICS_INTERVAL, useResource } from "@/hooks/use-resource";
import { filterParams } from "@/lib/filters";
import { parse, urls } from "@/lib/portfolio-api";

export function Analytics() {
  const { filters, set, pathname } = useUrlState();
  const live = filters.mode === "LIVE";
  const perf = useResource(live ? null : urls.performance(filterParams(filters, "all")), parse.performance, ANALYTICS_INTERVAL, { keepPrevious: "perf" });
  const range = filters.range ?? defaultRangeFor(pathname);
  return <div>
    <PageTitle title="Analytics" note={`${filters.mode} · closed trades only · computed by the API · IST session dates`}
      actions={<DateRangeSelector value={range} from={filters.from} to={filters.to} onChange={(r, from, to) => set({ range: r, from, to })}/>}/>
    <LiveGate live={live}>
      <ResourceView resource={perf} label="Analytics" skeletonRows={6}>{p => <PerformanceReport perf={p}/>}</ResourceView>
    </LiveGate>
  </div>;
}
