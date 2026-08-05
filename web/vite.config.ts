import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The build lands inside the Python package, so `pip install` ships the UI and a user
// never needs Node. `npm run dev` proxies the API to the daemon instead; start it with
// `council serve --dev-origin http://localhost:5173` so the origin guard lets it in.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../council/web',
    emptyOutDir: true,
    assetsDir: 'assets',
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: false,
      },
    },
  },
})
