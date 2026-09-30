import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emit a self-contained server in .next/standalone for the Docker image (P0-2).
  output: "standalone",
};

export default nextConfig;
