import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// Injects a Content-Security-Policy <meta> tag at build time. connect-src/img-src
// need the actual API origin, which varies by deployment (VITE_API_BASE_URL) -
// that can't be a static tag in index.html, so it's computed here instead.
// Note: <meta> CSP silently ignores directives that require a real HTTP header
// (frame-ancestors, report-to) - those need to be set at the hosting/reverse-proxy
// layer, outside this repo's scope.
function cspPlugin(apiOrigin) {
  const csp = [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    `img-src 'self' data: ${apiOrigin}`,
    `connect-src 'self' ${apiOrigin}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join('; ')

  return {
    name: 'inject-csp',
    transformIndexHtml: {
      // Must run before Vite's own asset-injection transforms and be the very
      // first thing in <head> - a <meta> CSP only covers elements that come
      // after it in the DOM, so anything injected ahead of it wouldn't be governed.
      order: 'pre',
      handler(html) {
        return html.replace('<head>', `<head>\n    <meta http-equiv="Content-Security-Policy" content="${csp}" />`)
      },
    },
  }
}

function apiOriginFrom(env) {
  try {
    return new URL(env.VITE_API_BASE_URL || 'http://localhost:8000/api').origin
  } catch {
    return 'http://localhost:8000'
  }
}

// https://vite.dev/config/
export default defineConfig(({ mode }) => ({
  plugins: [react(), cspPlugin(apiOriginFrom(loadEnv(mode, process.cwd(), '')))],
}))
