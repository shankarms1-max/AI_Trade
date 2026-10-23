import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import manifest from "@/app/manifest";
import { metadata, viewport } from "@/app/layout";

function pngSize(path: string): [number, number] {
  const bytes = readFileSync(path);
  expect(bytes.subarray(0, 8).toString("hex")).toBe("89504e470d0a1a0a");
  return [bytes.readUInt32BE(16), bytes.readUInt32BE(20)];
}

describe("iPhone web app metadata", () => {
  it("opens the dedicated monitor in standalone mode without offline caching", () => {
    const data = manifest();
    expect(data).toMatchObject({
      name: "AI Trade — Research Intelligence",
      short_name: "AI Trade",
      description: "Private NIFTY and Commodity paper research analytics",
      start_url: "/phase15", scope: "/", display: "standalone",
      background_color: "#0c0c13", theme_color: "#0c0c13",
    });
    expect(data.icons).toEqual([
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
    ]);
  });

  it("has the expected local PNG icon dimensions and Apple metadata", () => {
    expect(pngSize("public/icon-192.png")).toEqual([192, 192]);
    expect(pngSize("public/icon-512.png")).toEqual([512, 512]);
    expect(pngSize("public/apple-touch-icon.png")).toEqual([180, 180]);
    expect(metadata.manifest).toBe("/manifest.webmanifest");
    expect(metadata.appleWebApp).toMatchObject({ capable: true, title: "NIFTY Research", statusBarStyle: "black-translucent" });
    expect(metadata.icons).toMatchObject({ apple: [{ url: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" }] });
    expect(viewport).toMatchObject({ viewportFit: "cover", colorScheme: "dark light" });
    expect(viewport.themeColor).toEqual([{ media: "(prefers-color-scheme: dark)", color: "#0c0c13" }, { media: "(prefers-color-scheme: light)", color: "#f5f6fa" }]);
  });
});
