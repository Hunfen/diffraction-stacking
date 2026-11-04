import { useEffect, useRef, useState } from 'react';
import { wsUrlFor } from './backend';

export type WsStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

interface Options {
  enabled: boolean;
  onMessage: (ev: MessageEvent) => void;
  onStatusChange?: (status: WsStatus) => void;
}

/**
 * Auto-reconnecting WebSocket (exponential backoff: 1s -> 2s -> 4s -> ... capped at 30s, with jitter).
 * A change of path or enabled rebuilds the connection; unmounting cleans it up.
 */
export function useReconnectingWebSocket(path: string, options: Options): WsStatus {
  const { enabled, onMessage, onStatusChange } = options;
  const [status, setStatus] = useState<WsStatus>('idle');
  const onMessageRef = useRef(onMessage);
  const onStatusRef = useRef(onStatusChange);

  // The ref is synced in effects only, never written during render
  useEffect(() => {
    onMessageRef.current = onMessage;
    onStatusRef.current = onStatusChange;
  }, [onMessage, onStatusChange]);

  useEffect(() => {
    if (!enabled) return;

    let disposed = false;
    let ws: WebSocket | null = null;
    let connectTimer: ReturnType<typeof setTimeout> | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const updateStatus = (s: WsStatus) => {
      if (disposed) return;
      setStatus(s);
      onStatusRef.current?.(s);
    };

    const connect = () => {
      if (disposed) return;
      updateStatus(attempt === 0 ? 'connecting' : 'reconnecting');

      let url: string;
      try {
        url = wsUrlFor(path);
      } catch {
        updateStatus('closed');
        return;
      }

      try {
        ws = new WebSocket(url);
      } catch {
        scheduleReconnect();
        return;
      }

      ws.binaryType = 'blob'; // /ws/live JPEG frames arrive as Blobs, distinct from text messages

      ws.onopen = () => {
        attempt = 0;
        updateStatus('open');
      };

      ws.onmessage = (ev) => {
        if (!disposed) onMessageRef.current(ev);
      };

      ws.onclose = () => {
        if (!disposed) scheduleReconnect();
      };

      ws.onerror = () => {
        // onclose fires afterwards and goes through the shared reconnect path
        try {
          ws?.close();
        } catch {
          /* ignore */
        }
      };
    };

    const scheduleReconnect = () => {
      if (disposed) return;
      updateStatus('reconnecting');
      const base = Math.min(30000, 1000 * 2 ** attempt);
      attempt += 1;
      const delay = base + Math.round(Math.random() * 500);
      retryTimer = setTimeout(connect, delay);
    };

    // Defer the first connect to the next macrotask so a synchronous setState inside the effect cannot cascade renders
    connectTimer = setTimeout(connect, 0);

    return () => {
      disposed = true;
      if (connectTimer) clearTimeout(connectTimer);
      if (retryTimer) clearTimeout(retryTimer);
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
    };
  }, [path, enabled]);

  // When enabled=false, skip the state write and derive idle directly
  return enabled ? status : 'idle';
}
