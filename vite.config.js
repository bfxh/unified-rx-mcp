import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// RX-IDE Lite 前端构建。生产由 rxide/host.py(17310) 同源服务 dist/；
// 开发模式把 /api、/preview、/vendor 代理到后端 17310。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/api': { target: 'http://127.0.0.1:17310', changeOrigin: false },
      '/preview': { target: 'http://127.0.0.1:17310', changeOrigin: false },
      '/vendor': { target: 'http://127.0.0.1:17310', changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
  },
})
