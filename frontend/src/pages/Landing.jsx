import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../lib/AuthContext'

const FEATURES = [
  { icon: '🐛', title: 'Errors & bugs', desc: 'Syntax errors, logic bugs and edge cases caught before you ship.' },
  { icon: '🔒', title: 'Security scanning', desc: 'Hardcoded secrets, injection risks, unsafe eval and XSS exposure.' },
  { icon: '⚡', title: 'Performance', desc: 'Flags O(n²) loops, repeated queries and inefficient lookups.' },
  { icon: '✨', title: 'AI refactors', desc: 'One-click, AI-rewritten versions of your flagged code.' },
]

const LANGUAGES = ['PYTHON', 'JAVASCRIPT', 'JAVA', 'C++', 'TYPESCRIPT', 'GO', 'PHP']

const PLANS = [
  { name: 'Free', price: '$0', period: '', desc: '10 analyses / month, all languages.' },
  { name: 'Pro', price: '$19', period: '/mo', desc: 'Unlimited analyses, AI refactors, PDF reports.' },
  { name: 'Team', price: '$49', period: '/mo', desc: 'Shared history, team dashboards, priority support.' },
]

export default function Landing() {
  const navigate = useNavigate()
  const { user } = useAuth()
  const [snippet, setSnippet] = useState('')
  const [fileName, setFileName] = useState(null)
  const [selectedPlan, setSelectedPlan] = useState('Pro')
  const fileInputRef = useRef(null)

  const handleFileChange = (e) => {
    const file = e.target.files[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = (ev) => {
      setSnippet(ev.target.result)
      setFileName(file.name)
    }
    reader.readAsText(file)
  }

  const goAnalyze = () => {
    if (snippet.trim()) {
      sessionStorage.setItem('ca_pending_snippet', snippet)
      if (fileName) sessionStorage.setItem('ca_pending_filename', fileName)
    }
    navigate(user ? '/analyze' : '/register')
  }

  return (
    <div className="dotted-bg" style={{ paddingBottom: 80 }}>
      <div
        style={{
          maxWidth: 1280,
          margin: '0 auto',
          padding: '64px 48px 0',
          display: 'grid',
          gridTemplateColumns: '1.4fr 1fr',
          gap: 40,
          alignItems: 'start',
        }}
      >
        <div>
          <div
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 8,
              border: '1px solid #d8d8d3',
              borderRadius: 100,
              padding: '7px 14px',
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
              letterSpacing: '0.04em',
              color: '#3f3f3c',
              background: '#fff',
            }}
          >
            <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--color-accent)' }} />
            AI CODE REVIEW, INSTANTLY
          </div>
          <h1
            style={{
              fontFamily: 'var(--font-display)',
              fontWeight: 500,
              fontSize: 68,
              lineHeight: 1.04,
              letterSpacing: '-0.01em',
              margin: '24px 0 0',
              maxWidth: 780,
            }}
          >
            Find every{' '}
            <span style={{ background: 'var(--color-accent)', padding: '0 10px', borderRadius: 6 }}>bug</span> before
            your users do.
          </h1>
        </div>
        <p style={{ fontSize: 18, lineHeight: 1.5, color: '#4a4a45', margin: '78px 0 0', maxWidth: 360 }}>
          Paste your code or upload a file — get instant errors, security risks, performance flags and AI-written
          fixes, in seconds.
        </p>
      </div>

      <div style={{ maxWidth: 1280, margin: '0 auto', padding: '32px 48px 0', display: 'flex', alignItems: 'center', gap: 16 }}>
        <button className="btn btn-dark" style={{ padding: '16px 26px', fontSize: 15 }} onClick={goAnalyze}>
          Analyze your code
        </button>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            border: '1px solid #d8d8d3',
            borderRadius: 100,
            padding: '14px 20px',
            background: '#fff',
            color: '#8c8c85',
            fontSize: 15,
            minWidth: 280,
          }}
        >
          <span>🔍</span>
          <input
            type="text"
            placeholder={fileName ? `${fileName} loaded` : 'Paste a code snippet'}
            value={snippet}
            onChange={(e) => {
              setSnippet(e.target.value)
              setFileName(null)
            }}
            style={{ border: 'none', outline: 'none', width: '100%', fontSize: 15, color: '#171717', background: 'transparent' }}
          />
          <input
            type="file"
            ref={fileInputRef}
            style={{ display: 'none' }}
            onChange={handleFileChange}
            accept=".py,.js,.ts,.java,.cpp,.go,.php"
          />
          <button
            onClick={() => fileInputRef.current?.click()}
            title="Upload a file instead"
            style={{ border: 'none', background: 'none', cursor: 'pointer', color: '#8c8c85', fontSize: 16, flexShrink: 0, padding: 0 }}
          >
            📎
          </button>
        </div>
      </div>

      <div style={{ maxWidth: 1280, margin: '56px auto 0', padding: '0 48px' }}>
        <div
          style={{
            border: '1px solid var(--color-border-3)',
            borderRadius: 20,
            background: '#fff',
            boxShadow: 'var(--shadow-hero)',
            overflow: 'hidden',
            position: 'relative',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '16px 20px', borderBottom: '1px solid var(--color-border)' }}>
            <span style={{ width: 11, height: 11, borderRadius: '50%', background: '#f04747' }} />
            <span style={{ width: 11, height: 11, borderRadius: '50%', background: '#f0b429' }} />
            <span style={{ width: 11, height: 11, borderRadius: '50%', background: '#3fbf5f' }} />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 2, padding: '0 16px', borderBottom: '1px solid var(--color-border)', fontFamily: 'var(--font-mono)', fontSize: 13 }}>
            <span style={{ padding: '12px 16px', borderBottom: '2px solid #171717', color: '#171717' }}>api.py</span>
            <span style={{ padding: '12px 16px', color: '#a3a39c' }}>report.json</span>
            <span style={{ padding: '12px 16px', color: '#a3a39c' }}>config.yaml</span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 340px' }}>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 14, lineHeight: 1.9, padding: '26px 0', color: '#3f3f3c' }}>
              <div style={{ padding: '0 24px' }}><span style={{ color: '#c2c2bc', marginRight: 16 }}>1</span><span style={{ color: '#8c8c85' }}># run analysis</span></div>
              <div style={{ padding: '0 24px' }}><span style={{ color: '#c2c2bc', marginRight: 16 }}>2</span><span style={{ color: '#2563eb' }}>import</span> sqlite3</div>
              <div style={{ padding: '0 24px' }}><span style={{ color: '#c2c2bc', marginRight: 16 }}>3</span></div>
              <div style={{ padding: '0 24px', background: 'rgba(225,29,72,0.08)' }}><span style={{ color: '#c2c2bc', marginRight: 16 }}>4</span><span style={{ color: '#2563eb' }}>def</span> get_user(name):</div>
              <div style={{ padding: '0 24px', background: 'rgba(225,29,72,0.08)' }}><span style={{ color: '#c2c2bc', marginRight: 16 }}>5</span>&nbsp;&nbsp;q = <span style={{ color: '#059669' }}>"SELECT * WHERE user='"</span> + name</div>
              <div style={{ padding: '0 24px' }}><span style={{ color: '#c2c2bc', marginRight: 16 }}>6</span>&nbsp;&nbsp;<span style={{ color: '#2563eb' }}>return</span> db.execute(q)</div>
              <div style={{ padding: '0 24px' }}><span style={{ color: '#c2c2bc', marginRight: 16 }}>7</span></div>
              <div style={{ padding: '0 24px' }}><span style={{ color: '#c2c2bc', marginRight: 16 }}>8</span><span style={{ color: '#8c8c85' }}># complexity: 8/20</span></div>
            </div>
            <div style={{ borderLeft: '1px solid var(--color-border)', padding: 22, background: '#fafaf8' }}>
              <div style={{ border: '1px solid var(--color-border-3)', borderRadius: 14, background: '#fff', padding: 16, boxShadow: '0 10px 30px -18px rgba(0,0,0,0.3)' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <span style={{ fontSize: 13, fontWeight: 600 }}>Security issue found</span>
                  <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, background: 'rgba(225,29,72,0.12)', color: '#e11d48', padding: '3px 8px', borderRadius: 6 }}>HIGH</span>
                </div>
                <div style={{ fontSize: 13, color: '#5c5c56', marginTop: 8, lineHeight: 1.5 }}>SQL Injection risk — string-concatenated query on line 5.</div>
                <div style={{ fontFamily: 'var(--font-mono)', fontSize: 12, marginTop: 10, color: '#8c8c85' }}>Fix: use parameterized queries</div>
              </div>
              <div style={{ marginTop: 14, display: 'flex', alignItems: 'center', gap: 10 }}>
                <div style={{ width: 44, height: 44, borderRadius: '50%', background: 'conic-gradient(#8FF26B 288deg, #ececea 0deg)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <div style={{ width: 34, height: 34, borderRadius: '50%', background: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: 'var(--font-mono)', fontSize: 11, fontWeight: 700 }}>80</div>
                </div>
                <span style={{ fontSize: 12, color: '#8c8c85' }}>Overall score</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div id="features" style={{ maxWidth: 1280, margin: '96px auto 0', padding: '0 48px', display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 24 }}>
        {FEATURES.map((f) => (
          <div key={f.title} className="card" style={{ padding: 24 }}>
            <div style={{ fontSize: 22 }}>{f.icon}</div>
            <div style={{ fontWeight: 600, fontSize: 16, marginTop: 14 }}>{f.title}</div>
            <div style={{ fontSize: 14, color: 'var(--color-text-secondary)', marginTop: 6, lineHeight: 1.5 }}>{f.desc}</div>
          </div>
        ))}
      </div>

      <div
        id="languages"
        style={{
          maxWidth: 1280,
          margin: '96px auto 0',
          padding: '0 48px',
          display: 'flex',
          alignItems: 'center',
          gap: 44,
          flexWrap: 'wrap',
          fontFamily: 'var(--font-mono)',
          fontSize: 15,
          letterSpacing: '0.02em',
          color: '#b8b8b1',
        }}
      >
        {LANGUAGES.map((lang) => (
          <span key={lang}>{lang}</span>
        ))}
      </div>

      <div id="pricing" style={{ maxWidth: 1280, margin: '64px auto 0', padding: '0 48px' }}>
        <div style={{ fontFamily: 'var(--font-display)', fontSize: 32, fontWeight: 500 }}>Simple pricing</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 24, marginTop: 24 }}>
          {PLANS.map((plan) => {
            const selected = selectedPlan === plan.name
            return (
              <div
                key={plan.name}
                onClick={() => setSelectedPlan(plan.name)}
                style={{
                  border: `${selected ? 2 : 1}px solid ${selected ? '#171717' : '#ececea'}`,
                  borderRadius: 16,
                  padding: 28,
                  background: '#fff',
                  cursor: 'pointer',
                }}
              >
                <div style={{ fontWeight: 600, fontSize: 16 }}>{plan.name}</div>
                <div style={{ fontFamily: 'var(--font-display)', fontSize: 34, marginTop: 10 }}>
                  {plan.price}
                  <span style={{ fontSize: 14, color: '#8c8c85' }}>{plan.period}</span>
                </div>
                <div style={{ fontSize: 13, color: 'var(--color-text-secondary)', marginTop: 8 }}>{plan.desc}</div>
              </div>
            )
          })}
        </div>
      </div>

      <div
        style={{
          maxWidth: 1280,
          margin: '80px auto 0',
          padding: '28px 48px 0',
          borderTop: '1px solid var(--color-border)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          fontSize: 13,
          color: '#8c8c85',
        }}
      >
        <span>© 2026 Code Analyzer</span>
        <span style={{ fontFamily: 'var(--font-mono)' }}>Built for developers</span>
      </div>
    </div>
  )
}
