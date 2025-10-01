import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: { host: true, port: 5173 },
  define: {
    __API_BASE__: JSON.stringify(process.env.VITE_API_BASE || 'http://localhost:3000/api/v1')
  }
})
