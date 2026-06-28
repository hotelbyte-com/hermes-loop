import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import type { Db } from './client.ts'

const __dirname = dirname(fileURLToPath(import.meta.url))

// Idempotent: schema.sql uses CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS,
// so running this on every boot is safe and cheap.
export function migrate(db: Db): void {
  const schema = readFileSync(join(__dirname, 'schema.sql'), 'utf8')
  db.exec(schema)
}
