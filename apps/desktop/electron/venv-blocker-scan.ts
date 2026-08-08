'use strict'

/**
 * venv-blocker-scan.ts
 *
 * Thin helper that runs the Python venv-blocker scan as a subprocess and
 * returns a typed result for the Desktop update preflight.
 */

import { execFile } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface VenvBlockerProcess {
  pid: number
  name: string
  cmdline: string
}

export interface McpBridgeProcess extends VenvBlockerProcess {
  createdAt: number
}

export interface VenvBlockerScanResult {
  blocked: boolean
  processes: VenvBlockerProcess[]
  mcpBridges: McpBridgeProcess[]
  pausableGateways: number
}

export type ScanOutcome =
  | { kind: 'clear'; result: VenvBlockerScanResult }
  | { kind: 'blocked'; result: VenvBlockerScanResult }
  | { kind: 'probe-failure'; error: string }

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SCAN_TIMEOUT_MS = 15000
const SCAN_MODULE = 'hermes_cli._scan_venv_blockers'

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Strictly validate and parse the JSON output from the venv-blocker scan.
 * Pure function — no side effects.
 */
export function parseVenvBlockerScanOutput(raw: string): ScanOutcome {
  let parsed: any

  try {
    parsed = JSON.parse(raw)
  } catch {
    return { kind: 'probe-failure', error: 'malformed JSON' }
  }

  if (!parsed || typeof parsed !== 'object' || parsed.ok !== true) {
    return { kind: 'probe-failure', error: 'missing or invalid ok field' }
  }

  if (typeof parsed.blocked !== 'boolean') {
    return { kind: 'probe-failure', error: 'blocked must be a boolean' }
  }

  if (!Array.isArray(parsed.processes)) {
    return { kind: 'probe-failure', error: 'processes must be an array' }
  }

  const processes: VenvBlockerProcess[] = []

  for (const entry of parsed.processes) {
    if (!entry || typeof entry !== 'object') {
      return { kind: 'probe-failure', error: 'process entry must be an object' }
    }

    const { pid, name, cmdline } = entry

    if (!Number.isInteger(pid) || pid <= 0) {
      return { kind: 'probe-failure', error: 'process pid must be a positive integer' }
    }

    if (typeof name !== 'string' || name.length === 0) {
      return { kind: 'probe-failure', error: 'process name must be a non-empty string' }
    }

    if (typeof cmdline !== 'string') {
      return { kind: 'probe-failure', error: 'process cmdline must be a string' }
    }

    processes.push({ pid, name, cmdline })
  }

  // An older scanner knows nothing about MCP bridges. Treat its result as a
  // normal all-blocking scan rather than guessing which processes are safe to
  // offer for consent.
  const rawMcpBridges = parsed.mcp_bridges === undefined ? [] : parsed.mcp_bridges
  if (!Array.isArray(rawMcpBridges)) {
    return { kind: 'probe-failure', error: 'mcp_bridges must be an array' }
  }

  const mcpBridges: McpBridgeProcess[] = []
  for (const entry of rawMcpBridges) {
    if (!entry || typeof entry !== 'object') {
      return { kind: 'probe-failure', error: 'MCP bridge entry must be an object' }
    }
    const { pid, name, cmdline, created_at: createdAt } = entry
    if (!Number.isInteger(pid) || pid <= 0) {
      return { kind: 'probe-failure', error: 'MCP bridge pid must be a positive integer' }
    }
    if (typeof name !== 'string' || name.length === 0 || typeof cmdline !== 'string') {
      return { kind: 'probe-failure', error: 'MCP bridge name and cmdline must be strings' }
    }
    if (typeof createdAt !== 'number' || !Number.isFinite(createdAt) || createdAt <= 0) {
      return { kind: 'probe-failure', error: 'MCP bridge created_at must be a positive number' }
    }
    mcpBridges.push({ pid, name, cmdline, createdAt })
  }

  if (mcpBridges.some(bridge => processes.some(proc => proc.pid === bridge.pid))) {
    return { kind: 'probe-failure', error: 'a PID cannot be both a blocker and an MCP bridge' }
  }

  const pausableGateways = parsed.pausable_gateways === undefined ? 0 : parsed.pausable_gateways
  if (!Number.isInteger(pausableGateways) || pausableGateways < 0) {
    return { kind: 'probe-failure', error: 'pausable_gateways must be a non-negative integer' }
  }

  // Reject inconsistent combinations.
  if (parsed.blocked && processes.length + mcpBridges.length === 0) {
    return { kind: 'probe-failure', error: 'blocked is true but process list is empty' }
  }

  if (!parsed.blocked && (processes.length > 0 || mcpBridges.length > 0)) {
    return { kind: 'probe-failure', error: 'blocked is false but process list is non-empty' }
  }

  return parsed.blocked
    ? { kind: 'blocked', result: { blocked: true, processes, mcpBridges, pausableGateways } }
    : { kind: 'clear', result: { blocked: false, processes, mcpBridges, pausableGateways } }
}

/**
 * Run the venv-blocker scan subprocess.  Async so the Electron main-process
 * event loop is never blocked by the psutil process scan (up to 15s on a
 * loaded Windows box).  Accepts optional overrides for testing (dependency
 * injection).
 */
export async function scanVenvBlockers(
  updateRoot: string,
  execOverride?: typeof execFileAsync,
  resolveOverride?: typeof resolveVenvPython
): Promise<ScanOutcome> {
  const execFn = execOverride || execFileAsync
  const resolveFn = resolveOverride || resolveVenvPython
  let scanRoot: string
  try {
    scanRoot = fs.realpathSync.native(updateRoot)
  } catch {
    return { kind: 'probe-failure', error: 'update root could not be resolved' }
  }
  const venvPython = resolveFn(scanRoot)

  if (!venvPython) {
    return { kind: 'probe-failure', error: 'venv python not found' }
  }

  let stdout: string
  const env = { ...process.env }
  delete env.PYTHONPATH

  try {
    const proc = await execFn(venvPython, ['-m', SCAN_MODULE, '--root', scanRoot], {
      cwd: scanRoot,
      encoding: 'utf-8',
      timeout: SCAN_TIMEOUT_MS,
      windowsHide: true,
      env
    } as any)

    stdout = String((proc as any).stdout ?? '')
  } catch (err: any) {
    const diag = [`exit code ${err.status ?? err.code ?? -1}`]

    if (err.stderr) {
      diag.push(String(err.stderr).slice(0, 200))
    }

    return { kind: 'probe-failure', error: diag.join('; ') }
  }

  return parseVenvBlockerScanOutput(stdout)
}

/** Terminate one bridge the user explicitly consented to stop. */
export async function terminateMcpBridge(
  updateRoot: string,
  bridge: McpBridgeProcess,
  execOverride?: typeof execFileAsync,
  resolveOverride?: typeof resolveVenvPython
): Promise<boolean> {
  const execFn = execOverride || execFileAsync
  const resolveFn = resolveOverride || resolveVenvPython
  let scanRoot: string
  try {
    scanRoot = fs.realpathSync.native(updateRoot)
  } catch {
    return false
  }
  const venvPython = resolveFn(scanRoot)
  if (!venvPython) {
    return false
  }

  const env = { ...process.env }
  delete env.PYTHONPATH
  try {
    const proc = await execFn(
      venvPython,
      [
        '-m',
        SCAN_MODULE,
        '--root',
        scanRoot,
        '--terminate-mcp-bridge',
        String(bridge.pid),
        '--created-at',
        String(bridge.createdAt)
      ],
      { cwd: scanRoot, encoding: 'utf-8', timeout: SCAN_TIMEOUT_MS, windowsHide: true, env } as any
    )
    const parsed = JSON.parse(String((proc as any).stdout ?? ''))
    return parsed?.ok === true && parsed?.terminated === true
  } catch {
    return false
  }
}

// ---------------------------------------------------------------------------
// Internal helpers (exported for testing)
// ---------------------------------------------------------------------------

/** Resolve the venv python path.  Returns null if the file does not exist. */
export function resolveVenvPython(updateRoot: string): string | null {
  const isWindows = process.platform === 'win32'
  const pythonName = isWindows ? 'python.exe' : 'python3'
  const scriptsDir = isWindows ? 'Scripts' : 'bin'
  const candidate = path.join(updateRoot, 'venv', scriptsDir, pythonName)

  try {
    fs.accessSync(candidate)

    return candidate
  } catch {
    return null
  }
}

/**
 * Build a human-readable error message from blocker scan results.
 * Does NOT recommend --force-venv.
 */
export function formatBlockerMessage(result: VenvBlockerScanResult): string {
  const lines = [
    'Update aborted: another Hermes process is using this installation.',
    '',
    'These processes must be stopped before updating:',
    ''
  ]

  for (const proc of result.processes.slice(0, 10)) {
    lines.push(`  PID ${proc.pid}  ${proc.name}  ${proc.cmdline}`)
  }

  if (result.processes.length > 10) {
    lines.push(`  ... and ${result.processes.length - 10} more`)
  }

  if (result.mcpBridges.length > 0) {
    lines.push('')
    lines.push('Codex Hermes tool bridges are also using this installation:')
    for (const bridge of result.mcpBridges.slice(0, 5)) {
      lines.push(`  PID ${bridge.pid}  ${bridge.name}`)
    }
  }

  lines.push('')
  lines.push(
    'Close the terminal, app, or service owning that process.'
  )
  lines.push('Then retry the update.')

  return lines.join('\n')
}

/**
 * Build a probe-failure error message.
 */
export function formatProbeFailedMessage(): string {
  return (
    'Update aborted: Desktop could not verify the Hermes installation is free.\n' +
    '\n' +
    'Close other Hermes windows and terminals, then retry.  If the problem\n' +
    'persists, run `hermes update` in a terminal for detailed diagnostics.'
  )
}
