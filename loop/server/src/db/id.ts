import { randomBytes } from 'node:crypto'

// Prefixed, URL-safe IDs. Prefix makes diagnostics/logs readable (e.g. msg_..., agt_...).
export function newId(prefix: string): string {
  return `${prefix}_${randomBytes(9).toString('base64url')}`
}

export const now = (): number => Date.now()
