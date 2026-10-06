import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// HEKATE_UI_API_TARGET overrides the /api proxy target (default: the sisyphus Api on :5102).
// The local profile runs its Api on 127.0.0.1:5103 — see scripts/local/README.md.
// strictPort: fail instead of silently moving to another port.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5179,
    strictPort: true,
    proxy: {
      '/api': {
        target: process.env.HEKATE_UI_API_TARGET ?? 'http://localhost:5102',
        changeOrigin: true,
      },
    },
  },
})
