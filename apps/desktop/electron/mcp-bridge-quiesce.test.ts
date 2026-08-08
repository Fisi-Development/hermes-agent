import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { describe, it } from 'vitest'

import {
  clearMcpBridgeQuiesceMarker,
  isMcpBridgeQuiesceMarkerLive,
  mcpBridgeQuiesceMarkerPath,
  writeMcpBridgeQuiesceMarker
} from './mcp-bridge-quiesce'

describe('MCP bridge quiesce marker', () => {
  it('records its owning pid and does not let another Desktop clear it', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-quiesce-'))
    const marker = mcpBridgeQuiesceMarkerPath(home)

    try {
      assert.equal(writeMcpBridgeQuiesceMarker(home, 101), true)
      assert.equal(Number(fs.readFileSync(marker, 'utf8').split('\n')[0]), 101)

      clearMcpBridgeQuiesceMarker(home, 202)
      assert.equal(fs.existsSync(marker), true)

      clearMcpBridgeQuiesceMarker(home, 101)
      assert.equal(fs.existsSync(marker), false)
    } finally {
      fs.rmSync(home, { recursive: true, force: true })
    }
  })

  it('does not treat a stale or dead owner as a live bridge pause', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-quiesce-'))
    const marker = mcpBridgeQuiesceMarkerPath(home)

    try {
      fs.writeFileSync(marker, `999999\n${Math.floor(Date.now() / 1000)}\n`, 'utf8')
      assert.equal(isMcpBridgeQuiesceMarkerLive(home), false)

      fs.writeFileSync(marker, `1\n1\n`, 'utf8')
      assert.equal(isMcpBridgeQuiesceMarkerLive(home), false)
    } finally {
      fs.rmSync(home, { recursive: true, force: true })
    }
  })
})
