import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  turbopack: {
    // Pin the workspace root. Without it Turbopack walks up looking for a
    // lockfile, finds an unrelated one in the home directory, and warns on
    // every build.
    root: __dirname,
  },
};

export default nextConfig;
