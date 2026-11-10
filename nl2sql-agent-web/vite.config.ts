import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'

/** 后端地址；前端只发同源相对路径，由开发 / 预览服务器转发，避免跨域 */
const API_TARGET = 'http://127.0.0.1:8000'

/**
 * 需要转发到后端的接口前缀。
 * 新增模块（如 /retrieval）时在这里补一行；漏配的前缀会落到开发服务器的 SPA 回退上，
 * 请求会拿回 index.html，前端表现为「返回的是 HTML 而不是 JSON」。
 */
const apiProxy = {
  '/collection': { target: API_TARGET, changeOrigin: true },
  '/evaluation': { target: API_TARGET, changeOrigin: true },
  '/retrieval': { target: API_TARGET, changeOrigin: true },
  '/model': { target: API_TARGET, changeOrigin: true },
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    vueDevTools(),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: apiProxy,
  },
  preview: {
    proxy: apiProxy,
  },
})
