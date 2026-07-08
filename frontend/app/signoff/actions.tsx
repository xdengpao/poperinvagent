'use client'
import { useState } from 'react'
import { api } from '@/lib/api'

export default function SignoffActions({ itemId }: { itemId: string }) {
  const [status, setStatus] = useState('')
  async function act(action: string) {
    const reason = action === 'reject' ? (prompt('驳回理由（R17.2 必填）') ?? '') : ''
    if (action === 'reject' && !reason.trim()) { setStatus('驳回需理由'); return }
    try {
      await api.actSignoff(itemId, action, reason)
      setStatus(`已${action === 'approve' ? '签认' : '驳回'}`)
    } catch (e) {
      setStatus(`失败：${String(e)}`)
    }
  }
  return (
    <span>
      <button onClick={() => act('approve')}>签认</button>{' '}
      <button onClick={() => act('reject')}>驳回</button>{' '}
      <span style={{ color: '#888' }}>{status}</span>
    </span>
  )
}
