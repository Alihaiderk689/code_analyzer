import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { startConversation, getChatHistory, sendChatMessage, clearChatHistory } from '../lib/resources'
import { ApiError } from '../lib/api'

// Persisted, per-analysis "Chat with Your Code" panel - distinct from the
// floating global ChatWidget (that one is stateless and resets on refresh;
// this one loads/saves its history server-side via /api/chat/). Lives on its
// own page (see pages/AnalysisChat.jsx), linked from the Report page.
export default function AnalysisChatPanel({ analysisId, listHeight = 380 }) {
  const [conversationId, setConversationId] = useState(null)
  const [messages, setMessages] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState('')
  const [clearing, setClearing] = useState(false)
  const listRef = useRef(null)

  useEffect(() => {
    let cancelled = false

    async function load() {
      setLoading(true)
      setLoadError('')
      try {
        const conversation = await startConversation(analysisId)
        const history = await getChatHistory(conversation.id)
        if (cancelled) return
        setConversationId(conversation.id)
        setMessages(history)
      } catch (err) {
        if (!cancelled) setLoadError(err instanceof ApiError ? err.message : 'Could not load the conversation.')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    load()
    return () => {
      cancelled = true
    }
  }, [analysisId])

  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight
  }, [messages, sending])

  const handleSend = async (e) => {
    e.preventDefault()
    const text = input.trim()
    if (!text || sending || !conversationId) return

    // Optimistic: show the user's message immediately. Whether the AI call
    // below succeeds or fails, the backend always persists the user's message
    // first (see chat/views.py), so re-fetching history afterward in both
    // branches replaces this placeholder with the real, server-assigned rows.
    setMessages((prev) => [...prev, { id: `pending-${Date.now()}`, role: 'user', message: text }])
    setInput('')
    setSendError('')
    setSending(true)
    try {
      await sendChatMessage(conversationId, text)
      setMessages(await getChatHistory(conversationId))
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : 'AI service is currently unavailable.')
      // The user's message is usually still saved server-side even when the AI call
      // fails (see chat/views.py), so try to sync up - but if this fetch fails too
      // (e.g. the network itself is down), just leave the optimistic message in place.
      try {
        setMessages(await getChatHistory(conversationId))
      } catch {
        // Handled above via sendError; local optimistic state is the best we can show.
      }
    } finally {
      setSending(false)
    }
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend(e)
    }
  }

  const handleClear = async () => {
    if (!conversationId || messages.length === 0) return
    if (!window.confirm('Delete this chat? This cannot be undone.')) return
    setClearing(true)
    setSendError('')
    try {
      await clearChatHistory(conversationId)
      setMessages([])
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : 'Could not delete this chat.')
    } finally {
      setClearing(false)
    }
  }

  return (
    <div className="card" style={{ display: 'flex', flexDirection: 'column' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          justifyContent: 'space-between',
          padding: '16px 20px',
          borderBottom: '1px solid var(--color-border)',
        }}
      >
        <div>
          <div style={{ fontFamily: 'var(--font-display)', fontSize: 18, fontWeight: 500 }}>Chat with your code</div>
          <div style={{ fontSize: 12, color: 'var(--color-text-muted)', marginTop: 2 }}>
            Ask follow-up questions about this analysis — it remembers the conversation.
          </div>
        </div>
        {!loading && !loadError && messages.length > 0 && (
          <button
            className="btn-ghost"
            style={{ fontSize: 12, color: 'var(--color-danger)', whiteSpace: 'nowrap' }}
            disabled={clearing}
            onClick={handleClear}
          >
            {clearing ? 'Deleting…' : 'Delete chat'}
          </button>
        )}
      </div>

      {loadError ? (
        <div style={{ padding: 20 }}>
          <div className="msg-error">{loadError}</div>
        </div>
      ) : loading ? (
        <div style={{ padding: 20, display: 'flex', alignItems: 'center', gap: 10, color: 'var(--color-text-muted)', fontSize: 13 }}>
          <span className="spinner" /> Loading conversation…
        </div>
      ) : (
        <>
          <div ref={listRef} style={{ height: listHeight, overflowY: 'auto', padding: 20, display: 'flex', flexDirection: 'column', gap: 12 }}>
            {messages.length === 0 && (
              <div style={{ fontSize: 13, color: 'var(--color-text-muted)' }}>
                Ask something like "Why is issue #1 a problem?" or "Can you optimise this function?"
              </div>
            )}
            {messages.map((m) => (
              <ChatBubble key={m.id} role={m.role} message={m.message} />
            ))}
            {sending && (
              <div style={{ alignSelf: 'flex-start', display: 'flex', alignItems: 'center', gap: 8, color: 'var(--color-text-muted)', fontSize: 13 }}>
                <span className="spinner" /> Thinking…
              </div>
            )}
          </div>

          {sendError && (
            <div style={{ padding: '0 20px 12px' }}>
              <div className="msg-error">{sendError}</div>
            </div>
          )}

          <form
            onSubmit={handleSend}
            style={{ display: 'flex', gap: 10, padding: 16, borderTop: '1px solid var(--color-border)' }}
          >
            <input
              type="text"
              className="field"
              placeholder="Ask a question about this analysis…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={sending}
              style={{ flex: 1 }}
            />
            <button className="btn btn-dark" style={{ padding: '10px 20px', fontSize: 14 }} disabled={sending || !input.trim()}>
              Send
            </button>
          </form>
        </>
      )}
    </div>
  )
}

function ChatBubble({ role, message }) {
  const isUser = role === 'user'
  return (
    <div
      style={{
        alignSelf: isUser ? 'flex-end' : 'flex-start',
        maxWidth: '80%',
        background: isUser ? 'var(--color-text)' : 'var(--color-bg-subtle)',
        color: isUser ? 'var(--color-bg)' : 'var(--color-text)',
        borderRadius: 14,
        padding: '10px 14px',
        fontSize: 14,
        lineHeight: 1.6,
      }}
    >
      {isUser ? (
        <div style={{ whiteSpace: 'pre-wrap' }}>{message}</div>
      ) : (
        <div className="chat-markdown">
          <ReactMarkdown>{message}</ReactMarkdown>
        </div>
      )}
    </div>
  )
}
