import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const apiTarget = process.env.POWDRR_WORKSPACE_API ?? 'http://127.0.0.1:8765'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', (request) => {
            request.setHeader('Origin', apiTarget)
          })
        },
      },
    },
  },
})
