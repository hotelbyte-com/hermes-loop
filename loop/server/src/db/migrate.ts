import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import type { Db } from './client.ts'

const __dirname = dirname(fileURLToPath(import.meta.url))

// Idempotent: schema.sql uses CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS,
// so running this on every boot is safe and cheap.
//
// `machine` token columns are added via guarded ALTER (not in schema.sql) so existing
// M1 demo databases upgrade in place without a reset — CREATE TABLE IF NOT EXISTS
// cannot add columns to an already-created table.
export function migrate(db: Db): void {
  const schema = readFileSync(join(__dirname, 'schema.sql'), 'utf8')
  db.exec(schema)
  ensureColumn(db, 'machine', 'token_hash', 'TEXT')
  ensureColumn(db, 'machine', 'token_suffix', 'TEXT')
  db.exec('CREATE INDEX IF NOT EXISTS idx_machine_token ON machine(token_hash)')
}

function ensureColumn(db: Db, table: string, column: string, decl: string): void {
  // `table`/`column` are call-site literals, never user input — safe to interpolate.
  const cols = db.all<{ name: string }>(`PRAGMA table_info(${table})`)
  if (!cols.some((c) => c.name === column)) {
    db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${decl}`)
  }
}
