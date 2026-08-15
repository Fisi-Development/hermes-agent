import { randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

export const STAGED_UPDATER_BRIDGE_LEASE_ENV = 'HERMES_UPDATE_BRIDGE_LEASE_ID'
const LEASE_FILE = '.hermes-venv-quiesce'
const LEASE_SECONDS = 20 * 60
const HANDOFF_GRACE_SECONDS = 90

export interface TauriBridgeLease {
  leaseId: string
  raw: Buffer
}

/**
 * Publish the one-shot, parent-owned lease that the staged Tauri updater
 * adopts before it touches the managed venv. Exclusive link publication means
 * a concurrent or leftover handoff fails closed; it never overwrites evidence
 * from another updater.
 */
export function acquireTauriBridgeLease(
  hermesHome: string,
  installRoot: string,
  { now = () => Math.floor(Date.now() / 1000), ownerPid = process.pid, randomId = randomUUID } = {}
): TauriBridgeLease | null {
  const createdAt = now()
  const leaseId = randomId()

  if (
    !Number.isSafeInteger(createdAt) ||
    createdAt <= 0 ||
    !Number.isSafeInteger(ownerPid) ||
    ownerPid <= 0 ||
    !/^[A-Za-z0-9._-]{16,128}$/.test(leaseId)
  ) {
    return null
  }

  let canonicalInstallRoot: string

  try {
    canonicalInstallRoot = fs.realpathSync.native(installRoot)
  } catch {
    return null
  }

  const raw = Buffer.from(
    `${JSON.stringify({
      schema_version: 1,
      lease_id: leaseId,
      owner_pid: ownerPid,
      created_at: createdAt,
      expires_at: createdAt + LEASE_SECONDS,
      handoff_grace_until: createdAt + HANDOFF_GRACE_SECONDS,
      install_root: canonicalInstallRoot
    })}\n`,
    'utf8'
  )
  const leasePath = path.join(hermesHome, LEASE_FILE)
  const pendingPath = path.join(path.dirname(leasePath), `.hermes-lease-pending-${ownerPid}-${leaseId}`)

  try {
    fs.mkdirSync(path.dirname(leasePath), { recursive: true })
    fs.writeFileSync(pendingPath, raw, { flag: 'wx', mode: 0o600 })
    fs.linkSync(pendingPath, leasePath)

    return { leaseId, raw }
  } catch {
    return null
  } finally {
    try {
      fs.unlinkSync(pendingPath)
    } catch {
      // The only possible retained pending file is evidence of an interrupted
      // publication, which the Tauri updater deliberately treats as blocking.
    }
  }
}
