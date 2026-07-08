import { api } from '@/lib/api'

export const dynamic = 'force-dynamic'
const cell = { border: '1px solid #ccc', padding: '6px 12px', textAlign: 'left' as const }

export default async function ConnectivityPage() {
  let matrix: { adapter: string; ok: boolean; required: boolean; error: string }[] = []
  let mode = '未知'
  try {
    const c = await api.connectivity()
    matrix = c.matrix; mode = c.mode
  } catch { mode = '后端不可达' }
  return (
    <main>
      <h1>数据源连通性矩阵</h1>
      <p>系统模式：<b>{mode}</b>（生产必需源任一失败 → 拒绝意见生成模式，R1.2）</p>
      {matrix.length === 0 ? <p>无连通性数据（后端未执行自检或不可达）。</p> : (
        <table style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead><tr><th style={cell}>适配器</th><th style={cell}>状态</th><th style={cell}>必需</th><th style={cell}>说明</th></tr></thead>
          <tbody>{matrix.map((r) => (
            <tr key={r.adapter}>
              <td style={cell}>{r.adapter}</td>
              <td style={cell}>{r.ok ? '✅' : '❌'}</td>
              <td style={cell}>{r.required ? '是' : '否'}</td>
              <td style={cell}>{r.error || 'OK'}</td>
            </tr>
          ))}</tbody>
        </table>
      )}
    </main>
  )
}
