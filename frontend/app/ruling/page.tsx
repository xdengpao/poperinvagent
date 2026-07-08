import { api } from '@/lib/api'

export const dynamic = 'force-dynamic'
const cell = { border: '1px solid #ccc', padding: '6px 12px', textAlign: 'left' as const }

export default async function RulingPage() {
  let pending: { item_id: string; subject: string; issue: string }[] = []
  try {
    pending = (await api.rulingQueue()).pending
  } catch { /* 后端不可达 */ }
  return (
    <main>
      <h1>裁决队列</h1>
      <p style={{ color: '#666' }}>边缘判定/口径冲突待用户裁决；未决期间标的冻结扩权类动作（R5.3/R6.7）。</p>
      {pending.length === 0 ? <p>无待裁决条目。</p> : (
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead><tr><th style={cell}>标的</th><th style={cell}>争点</th></tr></thead>
          <tbody>{pending.map((i) => (
            <tr key={i.item_id}><td style={cell}>{i.subject}</td><td style={cell}>{i.issue}</td></tr>
          ))}</tbody>
        </table>
      )}
    </main>
  )
}
