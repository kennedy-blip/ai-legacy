import type { NextConfig } from "next";

// The browser only ever talks to this origin. In development and in the Docker stack,
// Next forwards /api/* to the backend named by ARCHAEO_BACKEND_URL. On Vercel the
// platform routes /api/* to the Python service before a request reaches Next (see
// vercel.json in the repository root), so no rewrite is registered there.
const backend = (process.env.ARCHAEO_BACKEND_URL ?? (process.env.VERCEL ? "" : "http://127.0.0.1:8000")).replace(
  /\/+$/,
  "",
);

const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
  poweredByHeader: false,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async rewrites() {
    if (!backend) return [];
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
