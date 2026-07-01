// E2E runtime-bridge tests — exercise the REAL bridge across real HTTP + real timing.
//
// The service-level dispatch.test.ts locks the CAS / renew / reaper invariants
// SYNCHRONOUSLY (manual claimed_at backdating, direct service calls). This file closes the
// three gaps those tests structurally cannot:
//
//   1. HTTP wiring — the actual Hono routes (poll / claim / renew / complete) + bearer auth,
//      exercised over a real socket (not in-process service calls).
//   2. The REAL machine client (cli/machine.ts) — its poll loop, renew-during-exec timer,
//      exec spawn, and complete path — driven as a real `node` subprocess against a live
//      server, exactly as `pnpm machine` runs in production.
//   3. Real timing — a long exec that EXCEEDS LOOP_CLAIM_TTL_MS, proving the client's renew
//      timer keeps the ORIGINAL claimant's lease fresh so a concurrent machine polling during
//      the exec does NOT take over (no wasted orphan exec), and exactly one agent reply lands.
//
// A real-LLM smoke (`claude -p`) is opt-in via LOOP_E2E_CLAUDE=1 (it costs a real API call +
// needs network/auth) so the default suite stays hermetic and free. The hard invariants
// (duplicate-reply = 0, renewal prevents takeover) are proven hermetically by tests A + B.
//
// Topology under test (all in-process except the machine client subprocess):
//   test process ── holds :memory: DB + Hono app on an ephemeral port
//                  └─ spawns `node cli/machine.ts` (one per machine) with cwd = per-machine
//                     tempdir holding that machine's .data/machine.json credential. The
//                     subprocess talks to the in-process server over real HTTP.

import assert from 'node:assert/strict'
import { spawn, type ChildProcess } from 'node:child_process'
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

import { serve } from '@hono/node-server'

import { buildApp } from '../api/routes.ts'
import { registerMachine, upsertInstance } from '../api/dispatch-service.ts'
import { newId, now } from '../db/id.ts'
import { createStore } from '../store.ts'
import type { BroadcastPolicy } from '../delivery/types.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const MACHINE_TS = resolve(HERE, '..', 'cli', 'machine.ts')
const QUIET: BroadcastPolicy = { defaultAudience: 'mentioned', allowAtAll: true, allowAtOnline: true }
const JSON_HEADER = { 'content-type': 'application/json' }
const RUN_CLAUDE = process.env.LOOP_E2E_CLAUDE === '1'

type Db = ReturnType<typeof createStore>
type Server = { port: number; close: () => Promise<void> }
type Machine = { id: string; token: string; tmpDir: string }

type World = {
  db: Db
  port: number
  wid: string
  cid: string
  aliceId: string
  specId: string
  machines: Machine[]
  server: Server
  children: ChildProcess[]
  logs: string[]
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

function setEnv(key: string, saved: string | undefined): void {
  if (saved === undefined) delete process.env[key]
  else process.env[key] = saved
}

async function startServer(db: Db): Promise<Server> {
  const app = buildApp(db)
  return new Promise((resolveFn, reject) => {
    const server = serve(
      { fetch: app.fetch, hostname: '127.0.0.1', port: 0 },
      (info) =>
        resolveFn({
          port: info.port,
          close: () =>
            new Promise<void>((done) => {
              // Drop any lingering keep-alive sockets so close() resolves promptly.
              ;(server as unknown as { closeAllConnections?: () => void }).closeAllConnections?.()
              server.close(() => done())
            }),
        }),
    )
    server.on('error', reject)
  })
}

async function makeWorld(opts: { machineCount: number; online: boolean[] }): Promise<World> {
  const db = createStore(':memory:')
  const wid = newId('ws')
  db.run('INSERT INTO workspace(id, slug, name, created_at) VALUES (?,?,?,?)', wid, 'e2e', 'E2E', now())
  const cid = newId('ch')
  db.run(
    'INSERT INTO channel(id, workspace_id, name, kind, broadcast_policy, context_scope, created_at) VALUES (?,?,?,?,?,?,?)',
    cid, wid, '#e2e', 'channel', JSON.stringify(QUIET), 'channel', now(),
  )
  // SpecBot — the single wake target.
  const soulId = newId('soul')
  db.run(
    'INSERT INTO soul(id, workspace_id, name, kind, role, description, created_at) VALUES (?,?,?,?,?,?,?)',
    soulId, wid, 'Spec Writer', 'agent', 'spec-writer', '', now(),
  )
  const specId = newId('agt')
  db.run(
    'INSERT INTO agent(id, workspace_id, soul_id, display_name, role, created_at) VALUES (?,?,?,?,?,?)',
    specId, wid, soulId, 'SpecBot', 'member', now(),
  )
  db.run('INSERT INTO channel_member(channel_id, member_id, member_kind) VALUES (?,?,?)', cid, specId, 'agent')
  // Alice — the human author of the waking message.
  const aliceId = newId('hum')
  db.run('INSERT INTO human(id, workspace_id, name, created_at) VALUES (?,?,?,?)', aliceId, wid, 'Alice', now())
  db.run('INSERT INTO channel_member(channel_id, member_id, member_kind) VALUES (?,?,?)', cid, aliceId, 'human')

  const server = await startServer(db)
  const baseUrl = `http://127.0.0.1:${server.port}`

  const machines: Machine[] = []
  for (let i = 0; i < opts.machineCount; i++) {
    const tmpDir = mkdtempSync(join(tmpdir(), `loop-e2e-mac${i + 1}-`))
    mkdirSync(join(tmpDir, '.data'), { recursive: true })
    const { machine, token } = registerMachine(db, wid, `mac${i + 1}`, null)
    const online = opts.online[i] ?? true
    upsertInstance(db, wid, specId, { machineId: machine.id, runtime: 'claude-code', online })
    machines.push({ id: machine.id, token, tmpDir })
    // Persist the credential the subprocess will read. chmod 0600 like the real writer.
    const cfgPath = join(tmpDir, '.data', 'machine.json')
    writeFileSync(cfgPath, JSON.stringify({ machineId: machine.id, token, baseUrl }, null, 2))
    try {
      chmodSync(cfgPath, 0o600)
    } catch {
      /* best-effort on non-POSIX */
    }
  }

  return { db, port: server.port, wid, cid, aliceId, specId, machines, server, children: [], logs: [] }
}

function spawnMachine(world: World, idx: number, opts: { exec?: string[]; interval?: number }): ChildProcess {
  const args = [MACHINE_TS, '--interval', String(opts.interval ?? 100)]
  if (opts.exec) args.push('--exec', ...opts.exec)
  const child = spawn(process.execPath, args, {
    cwd: world.machines[idx]!.tmpDir,
    env: { ...process.env },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const tag = `mac${idx + 1}`
  child.stdout?.on('data', (d: Buffer) => world.logs.push(`[${tag}] ${d.toString().trimEnd()}`))
  child.stderr?.on('data', (d: Buffer) => world.logs.push(`[${tag}!] ${d.toString().trimEnd()}`))
  world.children.push(child)
  return child
}

async function postWake(world: World, body: string): Promise<void> {
  const res = await fetch(`http://127.0.0.1:${world.port}/api/channels/${world.cid}/messages`, {
    method: 'POST',
    headers: JSON_HEADER,
    body: JSON.stringify({ body, authorId: world.aliceId, authorKind: 'human' }),
  })
  assert.equal(res.status, 201, `wake POST failed: ${res.status}`)
}

type AgentMsg = { id: string; body: string; authorHandle: string }

async function agentReplies(world: World): Promise<AgentMsg[]> {
  const res = await fetch(`http://127.0.0.1:${world.port}/api/channels/${world.cid}/messages`)
  const msgs = (await res.json()) as Array<{ authorKind: string; body: string; id: string; authorHandle: string }>
  return msgs.filter((m) => m.authorKind === 'agent').map((m) => ({ id: m.id, body: m.body, authorHandle: m.authorHandle }))
}

// Latest dispatch for SpecBot, read straight from the in-process DB (the single source of truth).
function dispatchOf(world: World): { state: string; claimedByMachine: string | null } | null {
  const r = world.db.get<{ state: string; claimed_by_machine: string | null }>(
    'SELECT state, claimed_by_machine FROM dispatch WHERE agent_id = ? ORDER BY created_at DESC LIMIT 1',
    world.specId,
  )
  return r ? { state: r.state, claimedByMachine: r.claimed_by_machine } : null
}

async function waitFor<T>(
  probe: () => T | null | Promise<T | null>,
  timeoutMs = 10_000,
  stepMs = 100,
  world?: World,
): Promise<T> {
  const start = Date.now()
  for (;;) {
    const v = await probe()
    if (v !== null && v !== undefined) return v
    if (Date.now() - start > timeoutMs) {
      const tail = world ? `\n--- machine logs ---\n${world.logs.slice(-30).join('\n')}` : ''
      throw new Error(`waitFor timed out after ${timeoutMs}ms${tail}`)
    }
    await sleep(stepMs)
  }
}

async function teardown(world: World): Promise<void> {
  for (const c of world.children) {
    if (!c.killed) {
      try {
        c.kill('SIGTERM')
      } catch {
        /* already gone */
      }
    }
  }
  await sleep(300) // let the client finish any in-flight exec / exit on SIGTERM
  for (const c of world.children) {
    if (!c.killed) {
      try {
        c.kill('SIGKILL')
      } catch {
        /* already gone */
      }
    }
  }
  await world.server.close()
  world.db.close()
  for (const m of world.machines) rmSync(m.tmpDir, { recursive: true, force: true })
}

// ---------- A: HTTP happy path via the real machine client (stub runtime) ----------

test('e2e A: real machine.ts (stub) drives the full HTTP bridge — exactly one agent reply', async () => {
  const world = await makeWorld({ machineCount: 1, online: [true] })
  try {
    await postWake(world, '@SpecBot draft the PRD')
    spawnMachine(world, 0, { interval: 100 }) // built-in stub runtime

    const reply = await waitFor(async () => {
      const rs = await agentReplies(world)
      return rs.length >= 1 ? rs[0]! : null
    }, 8_000, 100, world)
    assert.match(reply.body, /runtime-bridge stub/, 'the stub reply landed through the HTTP bridge')

    // Settle, then assert the no-duplicate invariant (the moat surface).
    await sleep(300)
    const all = await agentReplies(world)
    assert.equal(all.length, 1, 'exactly one agent reply — controllable delivery holds end-to-end')
    assert.equal(dispatchOf(world)?.state, 'done', 'dispatch reached terminal `done`')
  } finally {
    await teardown(world)
  }
})

// ---------- B: renewal keeps a long claim alive (real timing) ----------

test('e2e B: client renewal keeps a long claim alive — a concurrent machine does NOT take over', async () => {
  // exec (2s) far exceeds the 800ms claim TTL; the client renews every 400ms (min(ttl/2, 60s)).
  // Without those renewals hitting the server, machine B's polls (which run requeueStaleClaimed)
  // would reset machine A's stale claim and B would take over — wasting A's exec. This test
  // proves the renew timer actually fires over real HTTP+timing: A retains the claim, the reply
  // comes from A, and there is exactly one reply.
  const prevTtl = process.env.LOOP_CLAIM_TTL_MS
  process.env.LOOP_CLAIM_TTL_MS = '800'
  try {
    // machine A online, machine B OFFLINE initially → guarantees A is the sole claimant at the
    // start (no race for the pending dispatch before A has claimed).
    const world = await makeWorld({ machineCount: 2, online: [true, false] })
    try {
      await postWake(world, '@SpecBot go')
      spawnMachine(world, 0, { interval: 100, exec: ['sh', '-c', 'sleep 2; echo FROM_MAC_A'] })

      // Wait until A has claimed and is mid-exec (state=claimed), THEN bring B online + polling.
      await waitFor(
        () => (dispatchOf(world)?.state === 'claimed' ? dispatchOf(world) : null),
        6_000, 80, world,
      )

      // B comes online hosting the SAME agent and starts polling. It would take over the instant
      // A's lease goes stale — but A's renewals keep claimed_at fresh, so B never sees a pending
      // dispatch and stays idle.
      upsertInstance(world.db, world.wid, world.specId, {
        machineId: world.machines[1]!.id,
        runtime: 'claude-code',
        online: true,
      })
      spawnMachine(world, 1, { interval: 100 })

      const reply = await waitFor(
        async () => (await agentReplies(world)).find((r) => r.body === 'FROM_MAC_A') ?? null,
        8_000, 100, world,
      )
      assert.equal(reply.body, 'FROM_MAC_A', 'the reply came from the original claimant (A kept its lease)')

      // Settle to surface any stray takeover, then assert the no-duplicate invariant.
      await sleep(400)
      const all = await agentReplies(world)
      assert.equal(all.length, 1, 'no takeover → no second / orphan reply (duplicate-reply = 0)')

      const d = dispatchOf(world)
      assert.equal(d?.state, 'done')
      assert.equal(d?.claimedByMachine, world.machines[0]!.id, 'A retained the claim to completion')
    } finally {
      await teardown(world)
    }
  } finally {
    setEnv('LOOP_CLAIM_TTL_MS', prevTtl)
  }
})

// ---------- C: real `claude -p` runtime (opt-in; costs a real API call) ----------

;(RUN_CLAUDE ? test : test.skip)(
  'e2e C (LOOP_E2E_CLAUDE=1): real claude -p runtime round-trips one agent reply',
  async () => {
    const world = await makeWorld({ machineCount: 1, online: [true] })
    try {
      await postWake(world, '@SpecBot reply with exactly the word OK and nothing else')
      const t0 = Date.now()
      // The machine feeds the dispatch payload (JSON) on stdin; claude -p prints its reply to
      // stdout, which becomes the agent message body via the same complete path.
      spawnMachine(world, 0, { interval: 200, exec: ['claude', '-p'] })

      const reply = await waitFor(async () => (await agentReplies(world))[0] ?? null, 90_000, 500, world)
      const elapsed = Date.now() - t0
      assert.ok(reply.body.trim().length > 0, 'claude produced a non-empty reply')

      await sleep(500)
      const all = await agentReplies(world)
      assert.equal(all.length, 1, 'exactly one agent reply from the real runtime')
      assert.equal(dispatchOf(world)?.state, 'done')
      // eslint-disable-next-line no-console
      console.log(
        `[loop-e2e C] real claude -p round-trip: ${elapsed}ms; reply (${reply.body.length} chars): ${reply.body.slice(0, 80)}`,
      )
    } finally {
      await teardown(world)
    }
  },
)
