"use client";
import { RouteError } from "@/components/route-error";

export default function AstFuturesError({ retry, reset }: { error: Error & { digest?: string }; retry?: () => void; reset?: () => void }) {
  return <RouteError title="AST FUTURES" retry={retry} reset={reset}/>;
}
