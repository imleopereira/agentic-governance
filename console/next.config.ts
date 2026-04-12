import type { NextConfig } from "next";

const config: NextConfig = {
  env: {
    NEXT_PUBLIC_CONSOLE_UI_VERSION:
      process.env.NEXT_PUBLIC_CONSOLE_UI_VERSION ?? "v3",
  },
  // The console backend runs at localhost:8766. Proxy API requests
  // during development so the frontend doesn't need CORS in dev.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8766/api/:path*",
      },
    ];
  },
};

export default config;
