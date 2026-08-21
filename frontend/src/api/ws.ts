import { useEffect, useRef, useState } from 'react';
import { wsUrlFor } from './backend';

export type WsStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

interface Options {
  enabled: boolean;
  onMessage: (ev: MessageEvent) => void;
  onStatusChange?: (status: WsStatus) => void;
}

/**
 * 自动重连 WebSocket（指数退避：1s → 2s → 4s → … 上限 30s，带抖动）。
 * path 变化或 enabled 变化都会重建连接；卸载时清理。
 */
export function useReconnectingWebSocket(path: string, options: Options): WsStatus {
  const { enabled, onMessage, onStatusChange } = options;
  const [status, setStatus] = useState<WsStatus>('idle');
  const onMessageRef = useRef(onMessage);
  const onStatusRef = useRef(onStatusChange);

  // ref 只在 effect 中同步，不在渲染期写
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

      ws.binaryType = 'blob'; // /ws/live 的 JPEG 帧以 Blob 到达，与文本消息区分

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
        // onclose 会随后触发，统一走重连逻辑
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

    // 首次连接放到下一个宏任务，避免 effect 内同步 setState 造成级联渲染
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

  // enabled=false 时不写 state，直接派生出 idle
  return enabled ? status : 'idle';
}
