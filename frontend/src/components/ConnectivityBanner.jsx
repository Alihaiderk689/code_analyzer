import { useEffect, useState } from 'react'
import { checkHealth } from '../lib/resources'

export default function ConnectivityBanner() {
  const [unreachable, setUnreachable] = useState(false)

  useEffect(() => {
    checkHealth().catch(() => setUnreachable(true))
  }, [])

  if (!unreachable) return null

  return (
    <div
      style={{
        background: 'var(--color-danger)',
        color: '#fff',
        fontSize: 13,
        textAlign: 'center',
        padding: '8px 16px',
      }}
    >
      Can't reach the backend API — check that the server is running.
    </div>
  )
}
