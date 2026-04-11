import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/_api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/_api/, ''),
        // Strip APISIX headers to prevent spoofing (parity with nginx.conf)
        headers: {
          'X-Consumer-Username': '',
          'X-Consumer-Custom-Id': '',
        },
      },
    },
  },
})
