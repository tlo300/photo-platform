import type { NextConfig } from "next";

// Content-Security-Policy for the frontend.
// 'self' covers all same-origin resources. Inline styles are needed by Next.js
// for critical CSS injection; inline scripts are blocked.
// Extend this list as new trusted third-party origins are added.
const CSP = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' blob: data:",
  "font-src 'self'",
  "connect-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join("; ");

const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Content-Security-Policy", value: CSP },
];

const nextConfig: NextConfig = {
  // Allow dev-server resources (HMR) when accessed via 127.0.0.1 —
  // needed on Windows when another process occupies localhost:80 on IPv6.
  allowedDevOrigins: ["127.0.0.1"],

  async headers() {
    return [
      {
        // Apply to all routes
        source: "/(.*)",
        headers: securityHeaders,
      },
    ];
  },
};

export default nextConfig;
