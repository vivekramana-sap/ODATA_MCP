import type { NextConfig } from 'next'

const nextConfig: NextConfig = {
  async rewrites() {
    // In development, proxy /api/* to the Python configurator on port 7770
    return [
      {
        source: '/api/:path*',
        destination: 'http://localhost:7770/api/:path*',
      },
    ]
  },
}

export default nextConfig
