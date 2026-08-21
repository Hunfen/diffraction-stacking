/** 本地设置（localStorage 持久化）：预览 fps / 质量 / 缩放
 *  与后端 /api/live/start 钳制口径一致：fps 1–30 整数、quality 10–100 整数、
 *  scale ∈ {0.25, 0.5, 1.0}（后端校验枚举，非法值返回 400）。
 */

const KEYS = {
  fps: 'ds.preview.fps',
  quality: 'ds.preview.quality',
  scale: 'ds.preview.scale',
} as const;

export interface PreviewSettings {
  fps: number;
  quality: number;
  scale: number;
}

export const DEFAULT_PREVIEW: PreviewSettings = { fps: 15, quality: 80, scale: 1.0 };
export const ALLOWED_SCALES = [0.25, 0.5, 1.0] as const;

function readNumber(key: string, fallback: number): number {
  const raw = localStorage.getItem(key);
  if (raw === null) return fallback;
  const value = Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

/** scale 收敛到最接近的允许值 {0.25, 0.5, 1.0}（等价于后端枚举） */
export function snapScale(value: number): number {
  return ALLOWED_SCALES.reduce((best, allowed) =>
    Math.abs(allowed - value) < Math.abs(best - value) ? allowed : best,
  );
}

export function clampFps(value: number): number {
  return Math.min(30, Math.max(1, Math.round(value)));
}

export function clampQuality(value: number): number {
  return Math.min(100, Math.max(10, Math.round(value)));
}

export function normalizePreviewSettings(next: PreviewSettings): PreviewSettings {
  return {
    fps: clampFps(next.fps),
    quality: clampQuality(next.quality),
    scale: snapScale(next.scale),
  };
}

export function getPreviewSettings(): PreviewSettings {
  return normalizePreviewSettings({
    fps: readNumber(KEYS.fps, DEFAULT_PREVIEW.fps),
    quality: readNumber(KEYS.quality, DEFAULT_PREVIEW.quality),
    scale: readNumber(KEYS.scale, DEFAULT_PREVIEW.scale),
  });
}

export function setPreviewSettings(next: PreviewSettings): void {
  const normalized = normalizePreviewSettings(next);
  localStorage.setItem(KEYS.fps, String(normalized.fps));
  localStorage.setItem(KEYS.quality, String(normalized.quality));
  localStorage.setItem(KEYS.scale, String(normalized.scale));
}
