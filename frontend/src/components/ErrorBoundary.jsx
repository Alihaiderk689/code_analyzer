import { Component } from 'react'

// React error boundaries only catch render/lifecycle errors thrown by class or
// function components below them in the tree - they must be class components
// themselves (no hook equivalent exists). Without this, a bug in any page
// component would unmount the whole React tree and leave a blank white screen.
export default class ErrorBoundary extends Component {
  state = { error: null }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    console.error('Unhandled UI error:', error, info.componentStack)
  }

  handleReload = () => {
    this.setState({ error: null })
    window.location.assign('/')
  }

  render() {
    if (!this.state.error) return this.props.children

    return (
      <div
        style={{
          minHeight: '100vh',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 12,
          padding: 24,
          textAlign: 'center',
        }}
      >
        <div style={{ fontFamily: 'var(--font-display)', fontSize: 24, fontWeight: 500 }}>
          Something went wrong.
        </div>
        <div style={{ fontSize: 14, color: 'var(--color-text-secondary-2)', maxWidth: 420 }}>
          An unexpected error occurred. Try reloading the page — if it keeps happening, please let us know.
        </div>
        <button className="btn btn-dark" style={{ padding: '10px 20px', fontSize: 13, marginTop: 8 }} onClick={this.handleReload}>
          Reload
        </button>
      </div>
    )
  }
}
