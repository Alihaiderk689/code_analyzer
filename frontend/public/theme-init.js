// Sets the theme attribute before first paint so there's no flash of the
// wrong theme. Keep the 'ca_theme' key in sync with lib/ThemeContext.jsx -
// this can't share a JS constant with it since it runs before any module loads.
// Loaded as an external, non-deferred <script> (see index.html) rather than
// inlined, so a strict script-src 'self' CSP doesn't need an inline-script
// exception for it.
;(function () {
  try {
    var stored = localStorage.getItem('ca_theme')
    var theme =
      stored === 'light' || stored === 'dark'
        ? stored
        : window.matchMedia('(prefers-color-scheme: dark)').matches
          ? 'dark'
          : 'light'
    document.documentElement.dataset.theme = theme
  } catch {
    // localStorage can throw in some private-browsing modes - default light.
  }
})()
