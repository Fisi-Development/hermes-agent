/**
 * Guarantee that a detached updater is eventually able to replace the desktop.
 *
 * ``app.quit()`` is the preferred graceful path, but a quit listener or a
 * native modal can keep Electron resident after the handoff child starts. The
 * child then waits forever for the very PID that launched it. Arm a bounded
 * forced-exit fallback only after requesting the normal quit. The window
 * intentionally exceeds the SSH quit teardown budget in main.ts.
 */
export const HANDOFF_EXIT_FALLBACK_MS = 5_000

export interface HandoffExitDeps {
  exit: (code?: number) => void
  quit: () => void
  schedule?: (callback: () => void, delay: number) => { unref?: () => void }
}

export function scheduleHandoffExit(deps: HandoffExitDeps, dwellMs: number): void {
  const schedule = deps.schedule ?? ((callback, delay) => setTimeout(callback, delay))

  schedule(() => {
    deps.quit()
    const fallback = schedule(() => deps.exit(0), HANDOFF_EXIT_FALLBACK_MS)
    fallback.unref?.()
  }, dwellMs)
}
