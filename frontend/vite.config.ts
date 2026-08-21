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
      // Vite 8 基于 Rolldown：output.codeSplitting 等价于旧版 manualChunks，
      // 把 antd / recharts / react 系依赖拆成独立 vendor chunk，消除 >500kB 单 chunk 警告。
      rolldownOptions: {
        output: {
          codeSplitting: {
            groups: [
              {
                name: 'antd',
                test: /node_modules[\\/]antd[\\/]/,
                maxSize: 500 * 1024,
              },
              {
                name: 'antd-icons',
                test: /node_modules[\\/]@ant-design[\\/]/,
              },
              {
                name: 'rc-components',
                test: /node_modules[\\/](@rc-component|rc-[a-z-]+)[\\/]/,
              },
              {
                name: 'charts',
                test: /node_modules[\\/](recharts|d3-|victory-vendor|react-smooth|decimal\.js)[\\/]/,
              },
              {
                name: 'react-vendor',
                test: /node_modules[\\/](react|react-dom|react-router|react-router-dom|scheduler|axios|dayjs)[\\/]/,
              },
            ],
          },
        },
      },
    },
  };
});
