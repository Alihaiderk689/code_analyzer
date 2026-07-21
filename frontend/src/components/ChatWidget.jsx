import { useEffect, useRef, useState } from 'react'
import { matchPath, useLocation } from 'react-router-dom'
import { chatWithAi } from '../lib/resources'
import { ApiError } from '../lib/api'

export default function ChatWidget() {
  const location = useLocation()
  const [open, setOpen] = useState(false)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const listRef = useRef(null)

  const reportMatch = matchPath('/report/:id', location.pathname)
  const analysisId = reportMatch ? Number(reportMatch.params.id) : null

  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight
  }, [messages, open])

  const handleSend = async (e) => {
    e.preventDefault()
    const text = input.trim()
    if (!text || sending) return

    const history = messages.map(({ role, content }) => ({ role, content }))
    setMessages((prev) => [...prev, { role: 'user', content: text }])
    setInput('')
    setError('')
    setSending(true)
    try {
      const data = await chatWithAi(text, history, analysisId)
      setMessages((prev) => [...prev, { role: 'assistant', content: data.reply }])
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'AI service is currently unavailable.')
    } finally {
      setSending(false)
    }
  }

  return (
    <div style={{ position: 'fixed', right: 24, bottom: 24, zIndex: 100 }}>
      {open && (
        <div
          className="card"
          style={{
            width: 340,
            height: 460,
            marginBottom: 12,
            display: 'flex',
            flexDirection: 'column',
            boxShadow: 'var(--shadow-card)',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '14px 16px',
              borderBottom: '1px solid var(--color-border)',
            }}
          >
            <div>
              <div style={{ fontWeight: 600, fontSize: 14 }}>AI Assistant</div>
              {analysisId && (
                <div style={{ fontSize: 11, color: 'var(--color-text-muted)', fontFamily: 'var(--font-mono)' }}>
                  Chatting about analysis #{analysisId}
                </div>
              )}
            </div>
            <button
              onClick={() => setMessages([])}
              style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 12, color: 'var(--color-text-secondary-2)' }}
            >
              New chat
            </button>
          </div>

          <div ref={listRef} style={{ flex: 1, overflowY: 'auto', padding: 16, display: 'flex', flexDirection: 'column', gap: 10 }}>
            {messages.length === 0 && (
              <div style={{ fontSize: 13, color: 'var(--color-text-muted)' }}>
                Ask me anything about your code{analysisId ? ' or this analysis' : ''}.
              </div>
            )}
            {messages.map((m, i) => (
              <div
                key={i}
                style={{
                  alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start',
                  maxWidth: '85%',
                  background: m.role === 'user' ? '#171717' : '#f5f5f2',
                  color: m.role === 'user' ? '#fff' : '#171717',
                  borderRadius: 12,
                  padding: '8px 12px',
                  fontSize: 13,
                  lineHeight: 1.5,
                  whiteSpace: 'pre-wrap',
                }}
              >
                {m.content}
              </div>
            ))}
            {sending && <div style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>Thinking…</div>}
            {error && <div className="msg-error">{error}</div>}
          </div>

          <form onSubmit={handleSend} style={{ display: 'flex', gap: 8, padding: 12, borderTop: '1px solid var(--color-border)' }}>
            <input
              type="text"
              className="field"
              placeholder="Ask a question…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              disabled={sending}
            />
            <button className="btn btn-dark" style={{ padding: '10px 16px', fontSize: 13 }} disabled={sending || !input.trim()}>
              Send
            </button>
          </form>
        </div>
      )}

      <button
        onClick={() => setOpen((v) => !v)}
        style={{
          width: 52,
          height: 52,
          borderRadius: '50%',
          border: 'none',
          background: '#171717',
          color: '#fff',
          fontSize: 22,
          cursor: 'pointer',
          boxShadow: 'var(--shadow-card)',
          float: 'right',
        }}
        title="AI Assistant"
      >
        {open ? '×' : '💬'}
      </button>
    </div>
  )
}
