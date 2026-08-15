import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import { acquireTauriBridgeLease } from './tauri-bridge-lease'

const temporaryRoots: string[] = []

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    fs.rmSync(root, { force: true, recursive: true })
  }
})

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-tauri-lease-'))
  temporaryRoots.push(root)
  const home = path.join(root, 'home')
  const install = path.join(root, 'install')
  fs.mkdirSync(home)
  fs.mkdirSync(install)

  return { home, install }
}

test('publishes the Tauri bridge capability with the canonical install root', () => {
  const { home, install } = fixture()
  const lease = acquireTauriBridgeLease(home, install, {
    now: () => 1_786_778_000,
    ownerPid: 4242,
    randomId: () => 'bridge-lease-id-is-long-enough'
  })

  assert.ok(lease)
  const document = JSON.parse(fs.readFileSync(path.join(home, '.hermes-venv-quiesce'), 'utf8'))
  assert.equal(document.lease_id, lease.leaseId)
  assert.equal(document.owner_pid, 4242)
  assert.equal(document.install_root, fs.realpathSync.native(install))
  assert.equal(document.expires_at - document.created_at, 20 * 60)
  assert.equal(document.handoff_grace_until - document.created_at, 90)
})

test('fails closed rather than replacing another updater lease', () => {
  const { home, install } = fixture()
  const first = acquireTauriBridgeLease(home, install, {
    randomId: () => 'first-bridge-lease-is-long-enough'
  })
  assert.ok(first)

  const second = acquireTauriBridgeLease(home, install, {
    randomId: () => 'second-bridge-lease-is-long-enough'
  })
  assert.equal(second, null)
  assert.deepEqual(fs.readFileSync(path.join(home, '.hermes-venv-quiesce')), first.raw)
})
