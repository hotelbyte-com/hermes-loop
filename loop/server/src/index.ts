// Loop control plane entrypoint.
//
// Dev:  pnpm dev:server  -> http://127.0.0.1:8188  (web on :5188 proxies /api here)
// Prod: node src/index.ts serves both the API and the built web bundle (loop/web/dist).

import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { serve } from '@hono/node-server'
import { serveStatic } from '@hono/node-server/serve-static'
import { Hono } from 'hono'

import { buildApp } from './api/routes.ts'
import { createStore } from './store.ts'

const __dirname = dirname(fileURLToPath(import.meta.url))
const port = Number(process.env.PORT ?? 8188)
const db = createStore()

const root = new Hono()
root.route('/', buildApp(db))

// Serve the built collaboration UI if present (production single-port mode).
// API routes are registered above, so /api/* is matched before this catch-all.
// Anchor the web bundle to the module location (loop/server/src -> ../../web/dist),
// NOT process.cwd(), so launch directory cannot silently swap the served UI (review finding).
const webDist = resolve(__dirname, '..', '..', 'web', 'dist')
const indexHtmlPath = join(webDist, 'index.html')
if (existsSync(indexHtmlPath)) {
  const indexHtml = readFileSync(indexHtmlPath, 'utf8')
  root.get('/assets/*', serveStatic({ root: webDist }))
  root.get('/*', (c) => c.html(indexHtml))
  console.log(`[loop] serving web bundle from ${webDist}`)
} else {
  console.log('[loop] no built web bundle found — run `pnpm dev:web` (or `pnpm build:web`) for the UI')
}

serve({ fetch: root.fetch, port }, (info) => {
  console.log(`[loop] control plane on http://127.0.0.1:${info.port}`)
})
