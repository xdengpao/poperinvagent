import { api } from '@/lib/api'

export const dynamic = 'force-dynamic'

export default async function Home() {
  let mode = '未知'
  let allowed = false
  try {
    const c = await api.connectivity()
    mode = c.mode
    allowed = c.opinion_mode_allowed
  } catch {
    mode = '后端不可达'
  }
  return (
    <main>
      <h1>智能投资助手 · 概览</h1>
      <p>系统模式：<b>{mode}</b></p>
      <p>意见生成模式：<b>{allowed ? '允许' : '拒绝（degraded——生产必需源探测失败，R1.2）'}</b></p>
      <p style={{ color: '#666' }}>
        本界面为交互层（签认/裁决/连通性）。判定由后端确定性规则引擎执行（铁律 A1），
        前端不做任何投资判定。
      </p>
    </main>
  )
}
