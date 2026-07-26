import { defineConfig } from 'vitest/config'

// Deliberately standalone from vite.config.js: these tests only exercise
// plain JS modules (no React/JSX), so no plugins or DOM environment needed.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.js'],
  },
})
