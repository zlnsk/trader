/** @type {import('next').NextConfig} */
module.exports = {
  basePath: '/Trader',
  assetPrefix: '/Trader',
  poweredByHeader: false,
  output: 'standalone',
  eslint: { ignoreDuringBuilds: true },
  typescript: { ignoreBuildErrors: false },
};
