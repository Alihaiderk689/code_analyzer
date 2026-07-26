import { createContext, useCallback, useContext, useEffect, useState } from 'react'

// Keep in sync with the inline script in index.html, which sets this same key
// synchronously before first paint to avoid a flash of the wrong theme - this
// context takes over from there for anything set after initial load.
const THEME_KEY = 'ca_theme'

function readInitialTheme() {
  // index.html's inline script already resolved and applied the theme before
  // React ever mounts - just read back what it decided, so this doesn't have to
  // duplicate the localStorage/system-preference fallback logic.
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'
}

const ThemeContext = createContext(null)

export function ThemeProvider({ children }) {
  const [theme, setThemeState] = useState(readInitialTheme)

  const applyTheme = useCallback((next) => {
    document.documentElement.dataset.theme = next
    try {
      localStorage.setItem(THEME_KEY, next)
    } catch {
      // Private-browsing/quota-exceeded - theme still applies for this session.
    }
    setThemeState(next)
  }, [])

  const toggleTheme = useCallback(() => {
    applyTheme(theme === 'dark' ? 'light' : 'dark')
  }, [theme, applyTheme])

  // If the user never explicitly chose a theme, follow the OS preference live
  // (e.g. their system switches to dark mode at sunset).
  useEffect(() => {
    let hasExplicitChoice
    try {
      hasExplicitChoice = localStorage.getItem(THEME_KEY) != null
    } catch {
      hasExplicitChoice = false
    }
    if (hasExplicitChoice) return

    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const handleChange = (e) => applyTheme(e.matches ? 'dark' : 'light')
    media.addEventListener('change', handleChange)
    return () => media.removeEventListener('change', handleChange)
  }, [applyTheme])

  return (
    <ThemeContext.Provider value={{ theme, toggleTheme, setTheme: applyTheme }}>{children}</ThemeContext.Provider>
  )
}

export function useTheme() {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used within ThemeProvider')
  return ctx
}
