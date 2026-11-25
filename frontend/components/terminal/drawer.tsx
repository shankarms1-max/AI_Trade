"use client";
import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import { Icon } from "@/components/icons";

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])';

/**
 * Accessible modal detail drawer: role=dialog + aria-modal, labelled by its title, Esc closes, Tab is trapped,
 * backdrop click closes, focus returns to the element that opened it. Deep-linking is done by the caller (?position=<id>).
 */
export function DetailDrawer({ open, title, subtitle, onClose, children }: { open: boolean; title: ReactNode; subtitle?: ReactNode; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    return () => { previous?.focus?.(); };
  }, [open]);
  if (!open) return null;
  const onKey = (event: KeyboardEvent) => {
    if (event.key === "Escape") { event.stopPropagation(); onClose(); return; }
    if (event.key !== "Tab") return;
    const nodes = Array.from(ref.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []).filter(n => !n.hasAttribute("hidden"));
    if (!nodes.length) return;
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };
  return <div className="tm-drawer-root" onKeyDown={onKey}>
    <div className="tm-backdrop" onClick={onClose} aria-hidden="true"/>
    <div ref={ref} className="tm-drawer" role="dialog" aria-modal="true" aria-labelledby={titleId}>
      <header>
        <div style={{ flex: 1, minWidth: 0 }}><h2 id={titleId}>{title}</h2>{subtitle ? <div className="tm-muted">{subtitle}</div> : null}</div>
        <button type="button" className="tm-btn" data-autofocus aria-label="Close details" onClick={onClose}><Icon name="close" size={14}/>Close <kbd>Esc</kbd></button>
      </header>
      <div className="tm-drawer-body">{children}</div>
    </div>
  </div>;
}
