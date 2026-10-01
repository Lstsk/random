import type { NextConfig } from "next";

const backend = process.env.BACKEND_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // Proxy /api/* to the FastAPI service so the browser needs no CORS setup.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/:path*` }];
  },
  experimental: {
    // Network questions can take ~40 s (model planning + paced API fetches); default is 30 s.
    proxyTimeout: 120_000,
  },
};

export default nextConfig;
