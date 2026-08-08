/**
 * Durable, consent-driven update gate for Codex's Hermes-tools MCP bridges.
 *
 * Unlike the shared update marker, this file is intentionally bridge-specific:
 * it lets a running bridge exit and prevents a fresh Codex session from
 * remapping the target venv while the staged updater mutates it.
 */

import fs from 'node:fs'
import path from 'node:path'

export const MCP_BRIDGE_QUIESCE_MARKER = '.hermes-venv-quiesce'
const MAX_MARKER_AGE_MS = 20 * 60 * 1000

export function mcpBridgeQuiesceMarkerPath(hermesHome: string): string {
  return path.join(hermesHome, MCP_BRIDGE_QUIESCE_MARKER)
}

export function writeMcpBridgeQuiesceMarker(hermesHome: string, pid = process.pid): boolean {
  try {
    fs.writeFileSync(
      mcpBridgeQuiesceMarkerPath(hermesHome),
      `${pid}\n${Math.floor(Date.now() / 1000)}\n`,
      'utf8'
    )
    return true
  } catch {
    return false
  }
}

/** Clear the gate only after an update is no longer live, or after preflight aborts. */
export function clearMcpBridgeQuiesceMarker(hermesHome: string, ownerPid?: number): void {
  const marker = mcpBridgeQuiesceMarkerPath(hermesHome)
  try {
    if (Number.isInteger(ownerPid)) {
      const pid = Number.parseInt(fs.readFileSync(marker, 'utf8').split('\n')[0] || '', 10)
      if (pid !== ownerPid) {
        return
      }
    }
    fs.unlinkSync(marker)
  } catch {
    // Absent or not removable: the next normal startup/update will retry.
  }
}

/** The marker's declared owner still has the short-lived update lease. */
export function isMcpBridgeQuiesceMarkerLive(hermesHome: string, now = Date.now()): boolean {
  try {
    const [pidText, startedText] = fs.readFileSync(mcpBridgeQuiesceMarkerPath(hermesHome), 'utf8').split('\n')
    const pid = Number.parseInt(pidText || '', 10)
    const startedAt = Number.parseFloat(startedText || '') * 1000
    if (!Number.isInteger(pid) || pid <= 0 || !Number.isFinite(startedAt) || now < startedAt || now - startedAt > MAX_MARKER_AGE_MS) {
      return false
    }
    try {
      process.kill(pid, 0)
      return true
    } catch (error: any) {
      return error?.code === 'EPERM'
    }
  } catch {
    return false
  }
}
