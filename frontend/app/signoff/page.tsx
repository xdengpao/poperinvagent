import { api } from '@/lib/api'
import SignoffActions from './actions'

export const dynamic = 'force-dynamic'
const cell = { border: '1px solid #ccc', padding: '6px 12px', textAlign: 'left' as const }

export default async function SignoffPage() {
  let pending: { item_id: string; item_type: string; subject: string }[] = []
  try {
    pending = (await api.signoffQueue()).pending
  } catch { /* 后端不可达时空列表 */ }
  return (
    <main>
      <h1>签认队列</h1>
      <p style={{ color: '#666' }}>未签认对象不进入下游判定（R17.1）；驳回必须记录理由（R17.2）。</p>
      {pending.length === 0 ? <p>无待签认条目。</p> : (
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead><tr><th style={cell}>类型</th><th style={cell}>标的</th><th style={cell}>操作</th></tr></thead>
          <tbody>
            {pending.map((i) => (
              <tr key={i.item_id}>
                <td style={cell}>{i.item_type}</td>
                <td style={cell}>{i.subject}</td>
                <td style={cell}><SignoffActions itemId={i.item_id} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  )
}
