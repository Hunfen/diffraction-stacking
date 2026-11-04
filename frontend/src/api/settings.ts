/** Local settings (persisted in localStorage): preview fps / quality / scale
 *  Matches the clamping rules of /api/live/start: fps integer 1-30, quality integer 10-100,
 *  scale in {0.25, 0.5, 1.0} (the backend validates the enum and returns 400 otherwise).
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

/** Snap scale to the nearest allowed value {0.25, 0.5, 1.0} (mirrors the backend enum) */
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
