import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { apiGet, apiPost } from './client';
import { getPreviewSettings } from './settings';

/**
 * 全局 Acquistion 状态。
 *
 * 原来 /live 页内部管理 live 流的启停，现在把它提升到 App 级，让顶部导航栏
 * 的按钮和 Live 页内控件共享同一个状态。启停仍通过模块级队列串行化。
 */

export interface LiveStateResponse {
  streaming: boolean;
  error?: string | null;
  params?: Record<string, unknown>;
}

interface AcquisitionContextValue {
  streamStarted: boolean;
  acquisitionPending: boolean;
  startAcquisition: () => Promise<void>;
  stopAcquisition: () => Promise<void>;
  toggleAcquisition: (next: boolean) => void;
}

let liveLifecycle: Promise<void> = Promise.resolve();
function runLiveAction(action: () => Promise<void>): Promise<void> {
  const next = liveLifecycle.then(action, action);
  liveLifecycle = next.catch(() => undefined);
  return next;
}

const AcquisitionContext = createContext<AcquisitionContextValue | null>(null);

export function AcquisitionProvider({ children }: { children: ReactNode }) {
  const [streamStarted, setStreamStarted] = useState(false);
  const [acquisitionPending, setAcquisitionPending] = useState(false);
  const mountedRef = useRef(true);

  // 页面刷新后如果后端仍有流在跑，先同步一次开关状态。
  useEffect(() => {
    mountedRef.current = true;
    apiGet<LiveStateResponse>('/api/live/state', { silent: true })
      .then((state) => {
        if (mountedRef.current && typeof state.streaming === 'boolean') {
          setStreamStarted(state.streaming);
        }
      })
      .catch(() => {
        /* 后端不可用时保持默认关闭 */
      });
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const startAcquisition = useCallback(async () => {
    const params = getPreviewSettings();
    setAcquisitionPending(true);
    try {
      await runLiveAction(async () => {
        // 先确保没有残留流，再启动。
        await apiPost('/api/live/stop', undefined, { silent: true }).catch(() => undefined);
        if (!mountedRef.current) return;
        await apiPost('/api/live/start', params, { silent: true });
        if (!mountedRef.current) return;
        setStreamStarted(true);
      });
    } catch {
      if (mountedRef.current) setStreamStarted(false);
    } finally {
      if (mountedRef.current) setAcquisitionPending(false);
    }
  }, []);

  const stopAcquisition = useCallback(async () => {
    setAcquisitionPending(true);
    try {
      await runLiveAction(async () => {
        await apiPost('/api/live/stop', undefined, { silent: true }).catch(() => undefined);
        if (mountedRef.current) setStreamStarted(false);
      });
    } catch {
      if (mountedRef.current) setStreamStarted(false);
    } finally {
      if (mountedRef.current) setAcquisitionPending(false);
    }
  }, []);

  const toggleAcquisition = useCallback(
    (next: boolean) => {
      if (next === streamStarted || acquisitionPending) return;
      if (next) void startAcquisition();
      else void stopAcquisition();
    },
    [acquisitionPending, startAcquisition, stopAcquisition, streamStarted],
  );

  const value = useMemo<AcquisitionContextValue>(
    () => ({
      streamStarted,
      acquisitionPending,
      startAcquisition,
      stopAcquisition,
      toggleAcquisition,
    }),
    [
      streamStarted,
      acquisitionPending,
      startAcquisition,
      stopAcquisition,
      toggleAcquisition,
    ],
  );

  return <AcquisitionContext.Provider value={value}>{children}</AcquisitionContext.Provider>;
}

// oxlint-disable-next-line react/only-export-components
export function useAcquisition(): AcquisitionContextValue {
  const ctx = useContext(AcquisitionContext);
  if (!ctx) {
    throw new Error('useAcquisition must be used within AcquisitionProvider');
  }
  return ctx;
}
