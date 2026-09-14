import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    // 把 /api 请求代理到 FastAPI，前端代码里直接写相对路径 /api/xxx
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
