import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// dev 模式走 vite(5173),/api 代理到 Atlas 后端;生产模式 npm run build
// 后由 FastAPI 直接托管 web/dist,不需要 vite。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8321',
    },
  },
})
