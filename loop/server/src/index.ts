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
// Bind loopback by default. The control plane is meant to sit behind a reverse proxy on
// the same host (or run locally); exposing it directly to the network is opt-in via
// LOOP_BIND_HOST. This is also the trust anchor for the demo-only /api/seed gate
// (routes.ts): "loopback" is a real network fact here, not a forgeable Host header.
const hostname = process.env.LOOP_BIND_HOST ?? '127.0.0.1'
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

serve({ fetch: root.fetch, hostname, port }, (info) => {
  console.log(`[loop] control plane on http://${info.address ?? hostname}:${info.port}`)
})
