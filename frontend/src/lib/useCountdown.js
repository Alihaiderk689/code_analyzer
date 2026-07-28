import { useEffect, useState } from 'react'

export function formatCountdown(msRemaining) {
  const totalSeconds = Math.max(0, Math.floor(msRemaining / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const pad = (n) => String(n).padStart(2, '0')
  return hours > 0 ? `${hours}h ${pad(minutes)}m ${pad(seconds)}s` : `${minutes}m ${pad(seconds)}s`
}

// Ticks every second while `resetAt` (an ISO string, or null when not rate-limited)
// is in the future, so the "come back in..." message counts down live rather than
// showing a stale number. Once it reaches zero, re-checks with the server (rather
// than just assuming) in case usage shifted from another tab/device in the meantime.
export function useCountdown(resetAt, onExpire) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!resetAt) return
    const target = new Date(resetAt).getTime()
    if (Date.now() >= target) {
      onExpire()
      return
    }
    const interval = setInterval(() => {
      const current = Date.now()
      setNow(current)
      if (current >= target) {
        clearInterval(interval)
        onExpire()
      }
    }, 1000)
    return () => clearInterval(interval)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetAt])

  if (!resetAt) return null
  return Math.max(0, new Date(resetAt).getTime() - now)
}
