import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { submitAnalysis, uploadAnalysis } from '../lib/resources'
import { ApiError } from '../lib/api'

const LANGUAGES = ['Python', 'JavaScript', 'Java', 'C++', 'TypeScript', 'Go', 'PHP']

export default function NewAnalysis() {
  const navigate = useNavigate()
  const [mode, setMode] = useState('paste')
  const [name, setName] = useState('')
  const [code, setCode] = useState('')
  const [uploadFile, setUploadFile] = useState(null)
  const [selectedLanguage, setSelectedLanguage] = useState('Python')
  const [analyzing, setAnalyzing] = useState(false)
  const [error, setError] = useState('')
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef(null)
  const textareaRef = useRef(null)
  const gutterRef = useRef(null)

  const lineCount = code ? code.split('\n').length : 1

  const syncGutterScroll = () => {
    if (gutterRef.current && textareaRef.current) {
      gutterRef.current.scrollTop = textareaRef.current.scrollTop
    }
  }

  useEffect(() => {
    const pending = sessionStorage.getItem('ca_pending_snippet')
    if (pending) {
      setCode(pending)
      setMode('paste')
      sessionStorage.removeItem('ca_pending_snippet')
      sessionStorage.removeItem('ca_pending_filename')
    }
  }, [])

  const pickFile = (file) => {
    if (!file) return
    setUploadFile(file)
  }

  const handleAnalyze = async () => {
    setError('')
    if (mode === 'paste' && !code.trim()) {
      setError('Paste some code first.')
      return
    }
    if (mode === 'upload' && !uploadFile) {
      setError('Choose a file first.')
      return
    }

    setAnalyzing(true)
    try {
      const trimmedName = name.trim() || undefined
      let result
      let sourceForCache = code
      if (mode === 'paste') {
        result = await submitAnalysis(trimmedName, selectedLanguage, code)
      } else {
        sourceForCache = await uploadFile.text()
        result = await uploadAnalysis(uploadFile, trimmedName)
      }
      // The backend never returns source_code back to the client after this point,
      // so this is the only chance to keep it around for the Report page's diff view.
      try {
        sessionStorage.setItem(`ca_source_${result.id}`, sourceForCache)
      } catch {
        // sessionStorage can throw in private-browsing/quota-exceeded cases; diff view
        // just falls back to "no original source" in that case, nothing else depends on it.
      }
      navigate(`/report/${result.id}`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Analysis failed. Please try again.')
      setAnalyzing(false)
    }
  }

  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: '44px 40px 100px' }}>
      <div style={{ fontFamily: 'var(--font-display)', fontSize: 32, fontWeight: 500 }}>New analysis</div>
      <div style={{ fontSize: 14, color: 'var(--color-text-secondary-2)', marginTop: 4 }}>
        Paste your code, choose a language, and let AI review it.
      </div>

      <div
        style={{
          display: 'flex',
          gap: 8,
          marginTop: 28,
          border: '1px solid var(--color-border-2)',
          borderRadius: 100,
          padding: 4,
          width: 'fit-content',
        }}
      >
        <button
          style={{
            border: 'none',
            borderRadius: 100,
            padding: '9px 18px',
            fontSize: 13,
            cursor: 'pointer',
            background: mode === 'paste' ? '#171717' : 'transparent',
            color: mode === 'paste' ? '#fff' : '#171717',
          }}
          onClick={() => setMode('paste')}
        >
          Paste code
        </button>
        <button
          style={{
            border: 'none',
            borderRadius: 100,
            padding: '9px 18px',
            fontSize: 13,
            cursor: 'pointer',
            background: mode === 'upload' ? '#171717' : 'transparent',
            color: mode === 'upload' ? '#fff' : '#171717',
          }}
          onClick={() => setMode('upload')}
        >
          Upload file
        </button>
      </div>

      <input
        type="text"
        className="field"
        placeholder={mode === 'upload' ? 'Name (optional — defaults to the file name)' : 'Name this analysis (optional — e.g. "Student Report Script")'}
        value={name}
        onChange={(e) => setName(e.target.value)}
        style={{ marginTop: 18, maxWidth: 420 }}
      />

      {mode === 'paste' && (
        <div
          style={{
            display: 'flex',
            marginTop: 18,
            height: 260,
            border: '1px solid var(--color-border-2)',
            borderRadius: 14,
            overflow: 'hidden',
          }}
        >
          <div
            ref={gutterRef}
            style={{
              flexShrink: 0,
              padding: '18px 0',
              background: '#fafaf8',
              borderRight: '1px solid var(--color-border-2)',
              overflow: 'hidden',
              textAlign: 'right',
              userSelect: 'none',
            }}
          >
            {Array.from({ length: lineCount }, (_, i) => (
              <div
                key={i}
                style={{
                  padding: '0 10px',
                  fontFamily: 'var(--font-mono)',
                  fontSize: 13,
                  lineHeight: 1.6,
                  color: 'var(--color-text-muted)',
                }}
              >
                {i + 1}
              </div>
            ))}
          </div>
          <textarea
            ref={textareaRef}
            onScroll={syncGutterScroll}
            placeholder="Paste your code here..."
            value={code}
            onChange={(e) => setCode(e.target.value)}
            spellCheck={false}
            style={{
              flex: 1,
              height: '100%',
              border: 'none',
              outline: 'none',
              padding: 18,
              fontFamily: 'var(--font-mono)',
              fontSize: 13,
              lineHeight: 1.6,
              resize: 'none',
              whiteSpace: 'pre',
              overflow: 'auto',
            }}
          />
        </div>
      )}

      {mode === 'upload' && (
        <div
          onDragOver={(e) => {
            e.preventDefault()
            setDragOver(true)
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragOver(false)
            pickFile(e.dataTransfer.files[0])
          }}
          style={{
            marginTop: 18,
            border: `1.5px dashed ${dragOver ? '#171717' : 'var(--color-border-2)'}`,
            borderRadius: 14,
            padding: 50,
            textAlign: 'center',
          }}
        >
          <div style={{ fontSize: 30 }}>📁</div>
          <div style={{ fontSize: 14, marginTop: 10 }}>
            Drag &amp; drop or{' '}
            <span
              style={{ textDecoration: 'underline', cursor: 'pointer' }}
              onClick={() => fileInputRef.current?.click()}
            >
              choose a file
            </span>
          </div>
          {uploadFile && <div style={{ fontSize: 13, marginTop: 8, color: 'var(--color-success)' }}>{uploadFile.name}</div>}
          <div style={{ fontSize: 12, color: 'var(--color-text-muted)', marginTop: 6, fontFamily: 'var(--font-mono)' }}>
            .py .js .ts .java .cpp .go .php — max 2MB
          </div>
          <input
            ref={fileInputRef}
            type="file"
            style={{ display: 'none' }}
            onChange={(e) => pickFile(e.target.files[0])}
            accept=".py,.js,.jsx,.ts,.tsx,.java,.go,.rb,.php,.cs,.cpp,.cc,.c,.rs,.kt,.swift,.html,.css"
          />
        </div>
      )}

      <div style={{ marginTop: 26 }}>
        <div style={{ fontSize: 13, color: 'var(--color-text-secondary-2)', marginBottom: 10 }}>
          Language {mode === 'upload' && '(auto-detected from file extension)'}
        </div>
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          {LANGUAGES.map((lang) => {
            const active = selectedLanguage === lang
            return (
              <button
                key={lang}
                disabled={mode === 'upload'}
                onClick={() => setSelectedLanguage(lang)}
                style={{
                  border: `1px solid ${active ? '#171717' : 'var(--color-border-2)'}`,
                  background: active ? '#171717' : '#fff',
                  color: active ? '#fff' : '#171717',
                  borderRadius: 100,
                  padding: '9px 16px',
                  fontSize: 13,
                  cursor: mode === 'upload' ? 'default' : 'pointer',
                  opacity: mode === 'upload' ? 0.5 : 1,
                }}
              >
                {lang}
              </button>
            )
          })}
        </div>
      </div>

      {error && <div className="msg-error" style={{ marginTop: 20, width: 'fit-content' }}>{error}</div>}

      <button
        style={{
          marginTop: 32,
          background: '#171717',
          color: '#fff',
          border: 'none',
          borderRadius: 100,
          padding: '15px 28px',
          fontSize: 15,
          fontWeight: 600,
          cursor: analyzing ? 'default' : 'pointer',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
        }}
        disabled={analyzing}
        onClick={handleAnalyze}
      >
        {analyzing && <span className="spinner" />}
        {analyzing ? 'Analyzing...' : 'Analyze code'}
      </button>
    </div>
  )
}
