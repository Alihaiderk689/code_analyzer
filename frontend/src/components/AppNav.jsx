import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../lib/AuthContext'
import { useTheme } from '../lib/ThemeContext'

const NAV_LINKS = [
  { to: '/dashboard', label: 'Dashboard' },
  { to: '/analyze', label: 'New Analysis' },
  { to: '/history', label: 'History' },
  { to: '/github', label: 'GitHub' },
]

export default function AppNav() {
  const location = useLocation()
  const navigate = useNavigate()
  const { user, isAdmin, logout } = useAuth()
  const { theme, toggleTheme } = useTheme()
  const links = isAdmin ? [{ to: '/admin', label: 'Admin' }] : NAV_LINKS

  const displayName = user?.first_name || user?.username || user?.email || ''
  const initial = (displayName || 'U').charAt(0).toUpperCase()

  const handleLogout = async () => {
    await logout()
    navigate('/')
  }

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '18px 40px',
        borderBottom: '1px solid var(--color-border)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 36 }}>
        <div className="logo" onClick={() => navigate('/')}>
          <div className="logo-mark sm" />
          <span style={{ fontSize: 16, fontWeight: 600 }}>Code Analyzer</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 26, fontSize: 14 }}>
          {links.map((link) => {
            const active = location.pathname.startsWith(link.to)
            return (
              <Link
                key={link.to}
                to={link.to}
                style={{
                  color: active ? 'var(--color-text)' : 'var(--color-text-secondary-2)',
                  fontWeight: active ? 600 : 400,
                }}
              >
                {link.label}
              </Link>
            )
          })}
        </div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        <button
          onClick={toggleTheme}
          title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          style={{
            width: 30,
            height: 30,
            borderRadius: '50%',
            border: '1px solid var(--color-border-2)',
            background: 'none',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 14,
            cursor: 'pointer',
            color: 'var(--color-text)',
            padding: 0,
          }}
        >
          {theme === 'dark' ? '☀️' : '🌙'}
        </button>
        <div
          onClick={() => navigate('/settings')}
          title="Settings"
          style={{
            width: 30,
            height: 30,
            borderRadius: '50%',
            background: user?.avatar ? undefined : 'var(--color-accent)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 13,
            fontWeight: 700,
            fontFamily: 'var(--font-mono)',
            cursor: 'pointer',
            overflow: 'hidden',
          }}
        >
          {user?.avatar ? (
            <img src={user.avatar} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
          ) : (
            initial
          )}
        </div>
        <Link to="/settings" style={{ fontSize: 14, color: 'var(--color-text)' }}>
          {displayName}
        </Link>
        <button className="btn btn-outline" style={{ padding: '8px 16px', fontSize: 13 }} onClick={handleLogout}>
          Log out
        </button>
      </div>
    </div>
  )
}
