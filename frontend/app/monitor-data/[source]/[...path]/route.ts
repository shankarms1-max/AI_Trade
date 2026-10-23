import { readOnlyBridge } from "@/lib/read-only-bridge";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function GET(request: Request, context: { params: Promise<{ source: string; path: string[] }> }) {
  const { source, path } = await context.params;
  return readOnlyBridge(request, source, path);
}
