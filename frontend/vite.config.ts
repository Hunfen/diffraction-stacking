import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// dev 代理：/api、/api/files（HTTP）与 /ws（WebSocket）转发到后端地址。
// 目标优先级：VITE_BACKEND env > 默认 http://localhost:8000。
// 运行时修改的后端地址（/settings 页，localStorage）用于 WS / 图片 URL，直接拼接目标地址。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const backend = (env.VITE_BACKEND || 'http://localhost:8000').replace(/\/+$/, '');

  return {
    plugins: [react()],
    server: {
      proxy: {
        // 更具体的 /api/files 放在 /api 之前，两者都转发到后端
        '/api/files': {
          target: backend,
          changeOrigin: true,
        },
        '/api': {
          target: backend,
          changeOrigin: true,
        },
        '/ws': {
          target: backend,
          ws: true,
          changeOrigin: true,
        },
      },
    },
    build: {
      // 说明：曾尝试用 rolldownOptions.output.codeSplitting 手工拆分 antd/recharts vendor chunk，
      // 但 Rolldown 手工分组会制造跨 chunk 循环引用，生产运行时白屏（"M/I is not a function"）。
      // 因此改为默认代码拆分：正确性优先；chunk 体积告警可通过 chunkSizeWarningLimit 调整。
      chunkSizeWarningLimit: 2000,
    },
  };
});
