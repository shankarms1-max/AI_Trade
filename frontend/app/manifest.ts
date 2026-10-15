import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "NIFTY Research Terminal",
    short_name: "NIFTY Research",
    description: "Read-only NIFTY strategy research terminal",
    start_url: "/phase15",
    scope: "/",
    display: "standalone",
    background_color: "#08111A",
    theme_color: "#08111A",
    icons: [
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
  };
}
