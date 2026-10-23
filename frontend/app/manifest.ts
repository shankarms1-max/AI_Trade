import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "AI Trade — Research Intelligence",
    short_name: "AI Trade",
    description: "Private NIFTY and Commodity paper research analytics",
    start_url: "/phase15",
    scope: "/",
    display: "standalone",
    background_color: "#0c0c13",
    theme_color: "#0c0c13",
    icons: [
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
  };
}
