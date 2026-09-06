import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

import { describe, it } from 'vitest'

import { buildExactTerminateScript, parseTerminateScriptOutput } from './windows-process-terminate'

/**
 * 2026-09-06 review, P0-1: the scanner promoted any process whose argv merely
 * mentioned the venv to a "holder", main.ts passed `installRoot` only when a
 * Restart Manager resource was present, and the rendered script skipped the
 * whole authorization block when both claims were empty. A scanner-only
 * holder and its entire descendant tree were therefore job-killed on pid +
 * creation time alone. These are source-contract tests on the rendered
 * PowerShell; the real-PowerShell boundary suites exercise the mechanics.
 */
describe('exact terminate script: executable-under-install-root authorization', () => {
  const withRoot = buildExactTerminateScript(4242, 1_700_000_000, 500, { installRoot: 'C:\\hermes' })
  const withoutRoot = buildExactTerminateScript(4242, 1_700_000_000, 500)

  it('requires an install-root claim unconditionally', () => {
    for (const script of [withRoot, withoutRoot]) {
      assert.match(script, /if \(\[string\]::IsNullOrWhiteSpace\(\$installRootClaim\)\) \{\s*throw 'TERMINATION_AUTHORIZATION_CLAIM_MISSING'/)
      // The old gate let both-empty claims skip authorization entirely.
      assert.doesNotMatch(script, /-not \[string\]::IsNullOrWhiteSpace\(\$installRootClaim\) -or -not \[string\]::IsNullOrWhiteSpace\(\$resourceClaim\)/)
      assert.doesNotMatch(script, /TERMINATION_AUTHORIZATION_CLAIM_INCOMPLETE/)
    }

    assert.match(withoutRoot, /^\$installRootClaim = ''$/m)
  })

  it('proves the root image lives under the install root before the resource check', () => {
    const rootImage = withRoot.indexOf('$imagePath = [HermesForceReleaseNative]::ReadImagePath($rootHandle)')
    const rootUnderRoot = withRoot.indexOf('IsSameOrUnderRoot($imageFinal, $installRootFinal)')
    const resourceGate = withRoot.indexOf("if (-not [string]::IsNullOrWhiteSpace($resourceClaim)) {")
    const ownership = withRoot.indexOf('IsCurrentResourceOwner($resourceFinal, $pidTarget, $expectedUnix)')
    const rootAssign = withRoot.indexOf('$handles[[string]$pidTarget] = $rootHandle')

    assert.ok(rootImage > 0 && rootUnderRoot > rootImage, 'root image path is resolved then checked')
    assert.ok(resourceGate > rootUnderRoot, 'the resource claim is consulted only after the image proof')
    assert.ok(ownership > resourceGate && ownership < rootAssign, 'ownership re-proof is inside the resource gate, before assignment')
    assert.match(withRoot, /TERMINATION_EXECUTABLE_OUTSIDE_INSTALL_ROOT/)
    assert.match(withRoot, /TERMINATION_EXECUTABLE_IDENTITY_UNAVAILABLE/)
  })

  it('applies the same image proof to every descendant before it is assigned to the job', () => {
    const open = withRoot.indexOf('$childHandle = [HermesForceReleaseNative]::OpenAuthenticatedProcess($childPid')
    const image = withRoot.indexOf('$childImageFinal = [HermesForceReleaseNative]::ReadFinalPath([HermesForceReleaseNative]::ReadImagePath($childHandle))')
    const assign = withRoot.indexOf('$handles[[string]$childPid] = $childHandle')

    assert.ok(open > 0 && image > open && assign > image, 'descendant image is proven between open and assignment')
    assert.match(
      withRoot,
      /if \(\[string\]::IsNullOrWhiteSpace\(\$childImageFinal\) -or -not \[HermesForceReleaseNative\]::IsSameOrUnderRoot\(\$childImageFinal, \$installRootFinal\)\) \{\s*\[HermesForceReleaseNative\]::CloseHandle\(\$childHandle\) \| Out-Null\s*\[void\]\$foreign\.Add\(\$childPid\)\s*continue\s*\}/
    )
  })

  it('leaves foreign descendants alive and reports them on the success line', () => {
    assert.match(withRoot, /^\$foreign = New-Object 'System\.Collections\.Generic\.List\[int\]'$/m)
    assert.match(withRoot, /if \(\$foreign\.Count -gt 0\) \{ Write-Output \('FOREIGN_DESCENDANTS_LEFT_ALIVE pids=' \+ \(\$foreign -join ','\)\) \}/)
    assert.deepEqual(parseTerminateScriptOutput('TERMINATED\nFOREIGN_DESCENDANTS_LEFT_ALIVE pids=58196,58200', 0), {
      kind: 'terminated'
    })
  })
})

describe('main.ts force-release wiring', () => {
  const source = fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8')

  it('passes the install root for every holder, not only Restart Manager ones', () => {
    const start = source.indexOf('terminateHolder: async (holder, budgetMs, signal, deadlineAt) => {')
    assert.ok(start > 0, 'production terminateHolder callback present')
    const callback = source.slice(start, source.indexOf('\n  })', start))

    assert.match(callback, /return terminateWindowsHolderWithinDeadline\(holder, \{[\s\S]*?installRoot: updateRoot[\s\S]*?\}\)/)
    assert.doesNotMatch(callback, /proveResource/)
    assert.doesNotMatch(callback, /\.\.\.\(.*installRoot/)
  })
})
