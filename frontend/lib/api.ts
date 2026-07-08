// FastAPI 后端客户端（P3 任务 27）。
// 服务端组件（Node，无 origin）直连后端 BACKEND_URL；浏览器客户端走 /api 反向代理（next.config rewrites）。
export interface HealthResp { status: string; mode: string }
export interface ConnectivityRow { adapter: string; ok: boolean; required: boolean; error: string }
export interface Connectivity { mode: string; opinion_mode_allowed: boolean; matrix: ConnectivityRow[] }
export interface SignoffItem { item_id: string; item_type: string; subject: string }
export interface RulingItem { item_id: string; subject: string; issue: string }

// 服务端：直连后端绝对地址；浏览器：相对 /api（由 rewrites 代理到后端）
const base = (path: string) =>
  typeof window === 'undefined'
    ? `${process.env.BACKEND_URL || 'http://localhost:8000'}${path}`
    : `/api${path}`

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(base(path), { cache: 'no-store' })
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`)
  return r.json() as Promise<T>
}

export const api = {
  health: () => getJson<HealthResp>('/health'),
  connectivity: () => getJson<Connectivity>('/connectivity'),
  signoffQueue: () => getJson<{ pending: SignoffItem[] }>('/queues/signoff'),
  rulingQueue: () => getJson<{ pending: RulingItem[] }>('/queues/ruling'),
  async actSignoff(itemId: string, action: string, reason = '') {
    const r = await fetch(base('/queues/signoff/act'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_id: itemId, action, reason }),
    })
    if (!r.ok) throw new Error(await r.text())
    return r.json()
  },
}
