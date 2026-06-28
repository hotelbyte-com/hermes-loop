// Shared machine-client config (written by seed, read by the machine CLI).
//
// Location is CWD/.data/machine.json — same convention as the default DB path in
// store.ts, so `pnpm seed` and `pnpm machine` find each other when run from the same
// package dir. Contains an opaque bearer token; treat the file as a credential.

import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'

export type MachineConfig = {
  machineId: string
  token: string
  baseUrl: string
}

export function machineConfigPath(): string {
  return resolve(process.cwd(), '.data', 'machine.json')
}

export function readMachineConfig(): MachineConfig | undefined {
  const p = machineConfigPath()
  if (!existsSync(p)) return undefined
  return JSON.parse(readFileSync(p, 'utf8')) as MachineConfig
}

export function writeMachineConfig(cfg: MachineConfig): void {
  const p = machineConfigPath()
  mkdirSync(dirname(p), { recursive: true })
  writeFileSync(p, JSON.stringify(cfg, null, 2))
  // The file holds an opaque bearer credential — restrict to the owning uid so other
  // local users on a shared host cannot read it (review finding).
  try {
    chmodSync(p, 0o600)
  } catch {
    /* best-effort: non-POSIX filesystems (rare in this stack) */
  }
}
