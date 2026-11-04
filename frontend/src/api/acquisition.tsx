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
 * Global Acquistion state.
 *
 * The live stream used to be started and stopped inside the /live page. It now lives at App
 * level so the top-nav button and the Live page controls share one state. Toggles are still serialized by a module-level queue.
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

  // After a page refresh, sync the toggle once in case a stream is still running.
  useEffect(() => {
    mountedRef.current = true;
    apiGet<LiveStateResponse>('/api/live/state', { silent: true })
      .then((state) => {
        if (mountedRef.current && typeof state.streaming === 'boolean') {
          setStreamStarted(state.streaming);
        }
      })
      .catch(() => {
        /* Keep the default off state when the backend is unavailable */
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
        // Make sure no stale stream is left before starting.
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
