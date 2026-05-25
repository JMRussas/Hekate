import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { execSync } from 'child_process'

function getGitHash(): string {
  try {
    return execSync('git rev-parse --short HEAD').toString().trim()
  } catch {
    return 'unknown'
  }
}

export default defineConfig({
  plugins: [react()],
  define: {
    'import.meta.env.VITE_GIT_HASH': JSON.stringify(getGitHash()),
    'import.meta.env.VITE_BUILD_TIME': JSON.stringify(new Date().toISOString()),
  },
  server: {
    port: 5173,
    proxy: {
      '/api': process.env.VITE_ORCH_API ?? 'http://localhost:5200',
      '/context-api': {
        target: process.env.VITE_CONTEXT_API ?? 'http://localhost:5102',
        rewrite: (path) => path.replace(/^\/context-api/, '/api'),
      },
    },
  },
})
