/**
 * Backend address management.
 * - Defaults to http://localhost:8000
 * - Honors the VITE_BACKEND env var (the vite dev proxy target)
 * - Runtime changes (Settings page) persist to localStorage and apply immediately
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
 * Image / file URLs:
 * The contract uses the url field returned by the backend (a relative path).
 * - dev mode: join with the backend address from localStorage/env (a runtime change applies immediately; WS and images bypass the vite proxy)
 * - production mode: FastAPI serves frontend/dist, so a same-origin relative path is enough
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
 * WebSocket URLs:
 * - dev mode: point straight at the backend address (runtime switching supported)
 * - production mode: derive from window.location (wss/ws)
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
