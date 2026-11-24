"use client";
import { RouteError } from "@/components/route-error";

export default function McxFuturesError({ retry, reset }: { error: Error & { digest?: string }; retry?: () => void; reset?: () => void }) {
  return <RouteError title="MCX FUTURES" retry={retry} reset={reset}/>;
}
