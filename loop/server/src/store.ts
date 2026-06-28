// DB bootstrap shared by the server (index.ts) and the seed CLI (cli/seed.ts).

import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'

import { Db } from './db/client.ts'
import { migrate } from './db/migrate.ts'

export function createStore(dbPath?: string): Db {
  const path = dbPath ?? process.env.LOOP_DB_PATH ?? defaultPath()
  if (path !== ':memory:') {
    mkdirSync(dirname(path), { recursive: true })
  }
  const db = new Db(path)
  migrate(db)
  return db
}

function defaultPath(): string {
  // CWD is the server package dir when run via `pnpm --filter ./server ...`.
  return resolve(process.cwd(), '.data', 'loop.db')
}
