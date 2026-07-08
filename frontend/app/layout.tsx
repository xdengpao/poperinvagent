import type { ReactNode } from 'react'
import Disclaimer from '@/components/Disclaimer'

export const metadata = { title: '智能投资助手', description: '框架约束型智能投资助手交互层' }

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body style={{ fontFamily: 'system-ui, sans-serif', maxWidth: 960, margin: '0 auto', padding: 24 }}>
        <nav style={{ display: 'flex', gap: 16, marginBottom: 24, borderBottom: '2px solid #333', paddingBottom: 12 }}>
          <a href="/">概览</a>
          <a href="/signoff">签认队列</a>
          <a href="/ruling">裁决队列</a>
          <a href="/connectivity">数据源连通</a>
        </nav>
        {children}
        <Disclaimer />
      </body>
    </html>
  )
}
