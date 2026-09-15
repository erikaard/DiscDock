import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The Windows service serves this dashboard as plain static files. Keeping
  // the UI fully client-side means the installed app needs no Node runtime.
  output: "export",
  trailingSlash: true,
  images: {
    unoptimized: true,
  },
};

export default nextConfig;
