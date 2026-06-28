// Thin typed wrapper over node:sqlite DatabaseSync.
//
// Why node:sqlite: zero native compilation (ships with Node), synchronous semantics
// that keep the delivery decider simple, and good enough throughput for a single-node
// control plane. Swap point for Postgres later is isolated behind this class.

import { DatabaseSync } from 'node:sqlite'

export type Row = Record<string, unknown>

type StatementSync = ReturnType<DatabaseSync['prepare']>

export class Db {
  private readonly db: DatabaseSync
  private readonly stmts = new Map<string, StatementSync>()
  private txDepth = 0

  constructor(path: string) {
    this.db = new DatabaseSync(path)
    this.db.exec('PRAGMA journal_mode = WAL;')
    // synchronous=NORMAL is the WAL-recommended default (full is needlessly slow under WAL).
    this.db.exec('PRAGMA synchronous = NORMAL;')
    // busy_timeout: seed CLI / machine CLI / server can all open this file from separate
    // processes; without a wait, a second writer under WAL gets SQLITE_BUSY -> 500. Wait up
    // to 5s for the writer lock instead of failing fast (review finding).
    this.db.exec('PRAGMA busy_timeout = 5000;')
    this.db.exec('PRAGMA foreign_keys = ON;')
  }

  exec(sql: string): void {
    this.db.exec(sql)
  }

  prepare(sql: string): StatementSync {
    let s = this.stmts.get(sql)
    if (!s) {
      s = this.db.prepare(sql)
      this.stmts.set(sql, s)
    }
    return s
  }

  run(sql: string, ...params: unknown[]): { changes: number; lastInsertRowid: unknown } {
    const r = this.prepare(sql).run(...(params as never[])) as {
      changes: number
      lastInsertRowid: unknown
    }
    return { changes: r.changes, lastInsertRowid: r.lastInsertRowid }
  }

  get<T = Row>(sql: string, ...params: unknown[]): T | undefined {
    return this.prepare(sql).get(...(params as never[])) as T | undefined
  }

  all<T = Row>(sql: string, ...params: unknown[]): T[] {
    return this.prepare(sql).all(...(params as never[])) as T[]
  }

  // Reentrant via SAVEPOINT: a nested transaction(fn) call inside another opens a savepoint
  // instead of a second BEGIN (SQLite forbids nested BEGIN). Review finding: fragile contract.
  transaction<T>(fn: () => T): T {
    const depth = this.txDepth++
    const sp = `sp_${depth}`
    if (depth === 0) this.exec('BEGIN')
    else this.exec(`SAVEPOINT ${sp}`)
    try {
      const result = fn()
      if (depth === 0) this.exec('COMMIT')
      else this.exec(`RELEASE SAVEPOINT ${sp}`)
      return result
    } catch (err) {
      if (depth === 0) this.exec('ROLLBACK')
      else this.exec(`ROLLBACK TO SAVEPOINT ${sp}`)
      throw err
    } finally {
      this.txDepth--
    }
  }

  close(): void {
    this.db.close()
  }
}
