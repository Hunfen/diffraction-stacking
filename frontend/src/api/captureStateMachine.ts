import type { CaptureState } from './types';

/**
 * capture 状态事件序列过滤与 UI 语义（v1.1 船长口径）。
 *
 * 规则：
 * - 非阻塞 waiting_user（needs_user===false）绝不能替换阻塞确认 Modal；
 * - capturing 期间到达的任何非阻塞 waiting_user 都是陈旧事件；
 * - 用户 resume 到 capturing 后，同一相位/更早相位的阻塞 waiting_user 是延迟事件，
 *   不能重新打开 Modal。
 */
function sameCapturePhase(a: CaptureState, b: CaptureState): boolean {
  const pa = a.current_phase;
  const pb = b.current_phase;
  return Boolean(pa && pb && pa.type === pb.type && pa.index === pb.index);
}

export function shouldAcceptCaptureState(current: CaptureState, next: CaptureState): boolean {
  if (next.status !== 'waiting_user') return true;

  // 非阻塞 waiting_user 不能覆盖阻塞 LIGHT 确认点。
  if (next.needs_user === false && isBlockingWaiting(current)) return false;

  // capturing 期间到达的非阻塞 waiting_user 必为陈旧事件。
  if (next.needs_user === false && current.status === 'capturing') return false;

  // 用户已 resume 到 capturing 后，同一相位（或更早相位）的阻塞 waiting_user 是延迟事件。
  if (current.status === 'capturing' && next.needs_user !== false) {
    if (sameCapturePhase(current, next)) return false;
    const curIdx = current.current_phase?.index;
    const nextIdx = next.current_phase?.index;
    if (curIdx !== undefined && nextIdx !== undefined && nextIdx < curIdx) return false;
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
