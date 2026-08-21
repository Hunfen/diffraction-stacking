import type { CaptureState } from './types';

/**
 * capture 状态事件序列过滤与 UI 语义（v1.1 船长口径）。
 *
 * 只丢弃「当前 status=capturing 期间到达的、needs_user===false 的陈旧 waiting_user」；
 * needs_user===true 的 waiting_user（如 LIGHT 强制人工确认点）无论此前状态如何都必须接受，
 * 否则阻塞 Modal 会被误丢弃、拍摄流程卡死。
 */
export function shouldAcceptCaptureState(current: CaptureState, next: CaptureState): boolean {
  if (
    next.status === 'waiting_user' &&
    next.needs_user === false &&
    current.status === 'capturing'
  ) {
    return false;
  }
  return true;
}

/** waiting_user 且需要人工确认（needs_user 缺失按 true 处理，兼容旧后端）→ 阻塞 Modal */
export function isBlockingWaiting(state: CaptureState): boolean {
  return state.status === 'waiting_user' && state.needs_user !== false;
}

/** waiting_user 且不需要人工确认 → 非阻塞「正在准备拍摄」Banner */
export function isPreparingWaiting(state: CaptureState): boolean {
  return state.status === 'waiting_user' && state.needs_user === false;
}
