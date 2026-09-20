import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
	server: {
		port: 5173,
		strictPort: false,
		proxy: {
			'/api/core': {
				target: process.env.VITE_CORE_DEV_PROXY || 'http://127.0.0.1:8080',
				changeOrigin: true,
				rewrite: (path) => path.replace(/^\/api\/core/, ''),
			},
			'/api/knowledge': {
				target: process.env.VITE_KNOWLEDGE_DEV_PROXY || 'http://127.0.0.1:8090',
				changeOrigin: true,
			},
			'/api/rag': {
				target: process.env.VITE_RAG_DEV_PROXY || 'http://127.0.0.1:8000',
				changeOrigin: true,
				rewrite: (path) => path.replace(/^\/api\/rag/, ''),
			},
		},
	},
})
