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
          'X-UniBridge-Internal-Proxy': '',
        },
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('recharts')) return 'recharts';
          if (id.includes('keycloak-js')) return 'keycloak';
          if (id.includes('@tanstack/react-query')) return 'tanstack-query';
          if (id.includes('i18next') || id.includes('react-i18next')) return 'i18n';
          if (id.includes('react-router-dom') || id.match(/[\\/]react(-dom)?[\\/]/)) {
            return 'react-vendor';
          }
          return undefined;
        },
      },
    },
  },
})
