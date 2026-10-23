"use client";
import { useLayoutEffect, useRef, type ComponentProps } from "react";

/** One semantic table, priority record layout on mobile; raw books may opt into scrolling. */
export function ResponsiveTable({ children, ...props }: ComponentProps<"table">) {
  const ref = useRef<HTMLTableElement>(null);
  useLayoutEffect(() => {
    const table = ref.current;
    if (!table) return;
    const headings = Array.from(table.tHead?.rows[0]?.cells ?? []).map(cell => cell.textContent?.replace(/[↕↑↓]/g, "").trim() ?? "");
    for (const body of Array.from(table.tBodies)) for (const row of Array.from(body.rows)) {
      Array.from(row.cells).forEach((cell, index) => { cell.dataset.label = headings[index] ?? "Evidence"; });
    }
    const media=window.matchMedia?.("(max-width: 900px)");
    const focus=()=>{for(const button of Array.from(table.tHead?.querySelectorAll("button")??[]))button.tabIndex=media?.matches?-1:0;};
    focus();media?.addEventListener("change",focus);
    return ()=>media?.removeEventListener("change",focus);
  }, [children]);
  return <table ref={ref} role="table" data-layout="records" {...props}>{children}</table>;
}
