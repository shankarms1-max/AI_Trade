import { vi } from "vitest";

/**
 * Mutable fake router for next/navigation. Use as:
 *   vi.mock("next/navigation", async () => (await import("./helpers/nav-mock")).navigationMock());
 * (this file must not import application modules: the mock factory would deadlock on its own import).
 */
export const nav = { pathname: "/", search: "", pushes: [] as string[], replaces: [] as string[] };
const apply = (href: string) => { const [path, q = ""] = href.split("?"); nav.pathname = path || nav.pathname; nav.search = q; };
export const navigationMock = () => ({
  usePathname: () => nav.pathname,
  useSearchParams: () => new URLSearchParams(nav.search),
  useRouter: () => ({ push: (href: string) => { nav.pushes.push(href); apply(href); }, replace: (href: string) => { nav.replaces.push(href); apply(href); }, back: vi.fn(), prefetch: vi.fn() }),
  notFound: () => { throw new Error("NEXT_NOT_FOUND"); },
  redirect: (to: string) => { throw new Error(`NEXT_REDIRECT:${to}`); },
});
export const resetNav = (pathname = "/", search = "") => { nav.pathname = pathname; nav.search = search; nav.pushes = []; nav.replaces = []; };
