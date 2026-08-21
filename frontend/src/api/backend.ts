/**
 * 后端地址管理。
 * - 默认 http://localhost:8000
 * - 支持 env VITE_BACKEND（vite dev 代理目标）
 * - 运行时修改（/settings 页）持久化到 localStorage，立即生效
 */

export const DEFAULT_BACKEND = 'http://localhost:8000';

const STORAGE_KEY = 'ds.backend';

export function normalizeBackend(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return DEFAULT_BACKEND;
  return trimmed.replace(/\/+$/, '');
}

export function getBackend(): string {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored) return normalizeBackend(stored);
  const env = import.meta.env.VITE_BACKEND as string | undefined;
  if (env) return normalizeBackend(env);
  return DEFAULT_BACKEND;
}

export function setBackend(url: string): void {
  localStorage.setItem(STORAGE_KEY, normalizeBackend(url));
}

export function clearBackend(): void {
  localStorage.removeItem(STORAGE_KEY);
}

export function isAbsoluteUrl(url: string): boolean {
  return /^https?:\/\//i.test(url) || url.startsWith('blob:') || url.startsWith('data:');
}

/**
 * 图片 / 文件 URL：
 * 契约规定使用后端返回的 url 字段（相对路径）。
 * - dev 模式：直接用 localStorage/env 里的后端地址拼接（运行时修改后端地址后立即生效，WS/图片不经过 vite 代理）
 * - 生产模式：FastAPI 托管 frontend/dist，同源相对路径即可
 */
export function resolveUrl(url?: string | null): string {
  if (!url) return '';
  if (isAbsoluteUrl(url)) return url;
  if (import.meta.env.DEV) {
    return getBackend() + (url.startsWith('/') ? url : '/' + url);
  }
  return url.startsWith('/') ? url : '/' + url;
}

/**
 * WebSocket URL：
 * - dev 模式：直接指向后端地址（支持运行时切换）
 * - 生产模式：从 window.location 推导（wss/ws）
 */
export function wsUrlFor(path: string): string {
  const p = path.startsWith('/') ? path : '/' + path;
  if (import.meta.env.DEV) {
    const base = getBackend();
    const wsBase = base.replace(/^http/i, 'ws');
    return wsBase + p;
  }
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}${p}`;
}
