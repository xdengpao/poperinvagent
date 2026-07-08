/** @type {import('next').NextConfig} */
const nextConfig = {
  // 后端 FastAPI 地址经环境变量注入（默认本地 8000）；生产由反向代理统一域名
  async rewrites() {
    const api = process.env.BACKEND_URL || 'http://localhost:8000'
    return [{ source: '/api/:path*', destination: `${api}/:path*` }]
  },
}
export default nextConfig
