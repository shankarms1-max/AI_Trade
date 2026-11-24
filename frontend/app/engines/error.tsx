"use client";
import { RouteError } from "@/components/route-error";

export default function EnginesError({ retry, reset }: { error: Error & { digest?: string }; retry?: () => void; reset?: () => void }) {
  return <RouteError title="ENGINES" retry={retry} reset={reset}/>;
}
