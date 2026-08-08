import assert from 'node:assert/strict'

import { test } from 'vitest'

import { HANDOFF_EXIT_FALLBACK_MS, scheduleHandoffExit } from './handoff-exit'

test('handoff exit requests a graceful quit before its bounded forced-exit fallback', () => {
  const timers: Array<{ callback: () => void; delay: number; unrefCalls: number }> = []
  let quitCalls = 0
  const exits: number[] = []

  scheduleHandoffExit(
    {
      quit: () => {
        quitCalls += 1
      },
      exit: code => {
        exits.push(code ?? 0)
      },
      schedule: (callback, delay) => {
        const timer = { callback, delay, unrefCalls: 0 }
        timers.push(timer)
        return { unref: () => { timer.unrefCalls += 1 } }
      }
    },
    2_500
  )

  assert.deepEqual(timers.map(timer => timer.delay), [2_500])
  timers[0].callback()
  assert.equal(quitCalls, 1)
  assert.deepEqual(exits, [])
  assert.equal(timers[1].delay, HANDOFF_EXIT_FALLBACK_MS)
  assert.ok(HANDOFF_EXIT_FALLBACK_MS > 4_000)
  assert.equal(timers[1].unrefCalls, 1)

  timers[1].callback()
  assert.deepEqual(exits, [0])
})
