import { Link, useNavigate } from 'react-router-dom'
import LogoMark from './LogoMark'

export default function MarketingNav() {
  const navigate = useNavigate()

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '22px 48px',
        borderBottom: '1px solid var(--color-border)',
      }}
    >
      <div className="logo" onClick={() => navigate('/')}>
        <LogoMark size={28} />
        <span className="logo-word">Code Analyzer</span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 36, fontSize: 15, color: '#3f3f3c' }}>
        <a href="/#features">Features</a>
        <a href="/#languages">Languages</a>
        <a href="/#pricing">Pricing</a>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        <button className="btn-ghost" style={{ fontSize: 15, padding: '8px 4px' }} onClick={() => navigate('/login')}>
          Sign in
        </button>
        <Link to="/register" className="btn btn-dark">
          Start free
        </Link>
      </div>
    </div>
  )
}
