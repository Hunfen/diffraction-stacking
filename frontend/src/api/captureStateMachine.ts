import type { CaptureState } from './types';

/**
 * Capture state event filtering and UI semantics (v1.1 contract).
 *
 * Rules:
 * - a non-blocking waiting_user (needs_user===false) must never replace the blocking confirmation Modal;
 * - any non-blocking waiting_user arriving during capturing is a stale event;
 * - after the user resumes into capturing, a blocking waiting_user for the same or an earlier phase is a late event
 *   and must not reopen the Modal.
 */
function sameCapturePhase(a: CaptureState, b: CaptureState): boolean {
  const pa = a.current_phase;
  const pb = b.current_phase;
  return Boolean(pa && pb && pa.type === pb.type && pa.index === pb.index);
}

export function shouldAcceptCaptureState(current: CaptureState, next: CaptureState): boolean {
  if (next.status !== 'waiting_user') return true;

  // A non-blocking waiting_user must not cover the blocking LIGHT confirmation.
  if (next.needs_user === false && isBlockingWaiting(current)) return false;

  // A non-blocking waiting_user that arrives during capturing is always stale.
  if (next.needs_user === false && current.status === 'capturing') return false;

  // After resuming into capturing, a blocking waiting_user for the same (or an earlier) phase is late.
  if (current.status === 'capturing' && next.needs_user !== false) {
    if (sameCapturePhase(current, next)) return false;
    const curIdx = current.current_phase?.index;
    const nextIdx = next.current_phase?.index;
    if (curIdx !== undefined && nextIdx !== undefined && nextIdx < curIdx) return false;
  }

  return true;
}

/** waiting_user that needs manual confirmation (a missing needs_user counts as true for old backends) -> blocking Modal */
export function isBlockingWaiting(state: CaptureState): boolean {
  return state.status === 'waiting_user' && state.needs_user !== false;
}

/** waiting_user that needs no manual confirmation -> non-blocking "preparing capture" banner */
export function isPreparingWaiting(state: CaptureState): boolean {
  return state.status === 'waiting_user' && state.needs_user === false;
}
