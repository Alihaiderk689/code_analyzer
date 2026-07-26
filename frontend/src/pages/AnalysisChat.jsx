import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { getAnalysis } from '../lib/resources'
import { ApiError } from '../lib/api'
import AnalysisChatPanel from '../components/AnalysisChatPanel'

export default function AnalysisChat() {
  const { id } = useParams()
  const [analysis, setAnalysis] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    getAnalysis(id)
      .then(setAnalysis)
      .catch((err) => setError(err instanceof ApiError ? err.message : 'Could not load this analysis.'))
  }, [id])

  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: '40px 40px 100px' }}>
      <Link to={`/report/${id}`} style={{ fontSize: 14, color: 'var(--color-text-secondary)' }}>
        ← Back to report
      </Link>

      {error ? (
        <div className="msg-error" style={{ marginTop: 20 }}>{error}</div>
      ) : (
        <>
          <div style={{ marginTop: 14 }}>
            {analysis && (
              <div style={{ fontSize: 13, color: 'var(--color-text-secondary-2)', fontFamily: 'var(--font-mono)' }}>
                {analysis.language}
              </div>
            )}
            <div style={{ fontFamily: 'var(--font-display)', fontSize: 26, fontWeight: 500, marginTop: 4 }}>
              {analysis ? `Chat — ${analysis.name}` : 'Chat with your code'}
            </div>
          </div>

          <div style={{ marginTop: 24 }}>
            <AnalysisChatPanel analysisId={id} listHeight={480} />
          </div>
        </>
      )}
    </div>
  )
}
