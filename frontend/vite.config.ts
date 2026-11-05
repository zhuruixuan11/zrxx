import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      // 把 /api/* 转发到后端 FastAPI
      '/api': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
  build: {
    // Element Plus 本身较大，单独成 chunk 后放宽告警阈值
    chunkSizeWarningLimit: 1200,
    // 体积优化：把大依赖拆成独立 chunk，便于浏览器缓存
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (/node_modules[\\/](echarts|vue-echarts|zrender)/.test(id)) return 'echarts'
          if (/node_modules[\\/](element-plus|@element-plus)/.test(id)) return 'element-plus'
          if (/node_modules[\\/](vue|vue-router|pinia|@vue)[\\/]/.test(id)) return 'vue'
          return undefined
        },
      },
    },
  },
})
