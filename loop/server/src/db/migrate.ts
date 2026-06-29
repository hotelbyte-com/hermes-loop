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

  // D-026: task<->dispatch combo. dispatch.task_id anchors the assignment dispatch to its
  // task; task.assignment_message_id is the immutable audit projection of the synthesized
  // assignment message; agent.role discriminates the seeded system ghost author ('system')
  // from ordinary members ('member'). Added via guarded ALTER (not schema.sql alone) so
  // existing M1/M2/M3 databases upgrade in place without a reset.
  ensureColumn(db, 'dispatch', 'task_id', 'TEXT REFERENCES task(id) ON DELETE SET NULL')
  ensureColumn(db, 'task', 'assignment_message_id', 'TEXT REFERENCES message(id) ON DELETE SET NULL')
  // SQLite ALTER ADD COLUMN cannot attach a CHECK constraint; role gets CHECK only in
  // schema.sql (fresh DBs). The ALTER path relies on app-level values (always 'member' |
  // 'system') — see seed/pm-scenario.ts + task-service.findSystemAgent.
  ensureColumn(db, 'agent', 'role', "TEXT NOT NULL DEFAULT 'member'")
  db.exec('CREATE INDEX IF NOT EXISTS idx_dispatch_task ON dispatch(task_id)')
}

function ensureColumn(db: Db, table: string, column: string, decl: string): void {
  // `table`/`column` are call-site literals, never user input — safe to interpolate.
  const cols = db.all<{ name: string }>(`PRAGMA table_info(${table})`)
  if (!cols.some((c) => c.name === column)) {
    db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${decl}`)
  }
}
