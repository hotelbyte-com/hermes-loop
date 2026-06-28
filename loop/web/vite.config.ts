import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: Vite on 5188 proxies /api to the Loop control plane on 8188.
// Prod: server serves the built web bundle from loop/web/dist.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5188,
    proxy: {
      '/api': 'http://127.0.0.1:8188',
    },
  },
  build: { outDir: 'dist', sourcemap: true },
})
