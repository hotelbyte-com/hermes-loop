// Regression tests locking the product MOAT (D-022 controllable delivery + D-024 runtime
// bridge). These are the invariants a refactor must not silently break:
//   - quiet default / DEFERRED_OFFLINE produce NO dispatch (controllable boundary)
//   - a direct @mention of an online agent spawns a pending dispatch
//   - an offline direct-mention is QUEUED (pending), invisible to a machine until online
//   - claim CAS rejects double-claim; complete posts a real agent reply
//   - a reply re-enters the decider: @mention in a reply spawns a nested dispatch
//   - cross-workspace isolation: a foreign machine token cannot claim (404, no leak)
//
// Uses node:test (built-in, zero deps) against an in-memory node:sqlite DB.

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

import { newId, now } from '../db/id.ts'
import { createStore } from '../store.ts'
import { postMessage } from '../api/delivery-service.ts'
import {
  abandonDispatch,
  authMachineByToken,
  claimDispatch,
  claimTtlMs,
  completeDispatch,
  HttpError,
  instanceTtlMs,
  pollDispatches,
  reapStaleInstances,
  registerMachine,
  renewClaim,
  requeueStaleClaimed,
  upsertInstance,
} from '../api/dispatch-service.ts'
import { assignTask, cancelTask, createTask, getTask } from '../api/task-service.ts'
import type { BroadcastPolicy } from '../delivery/types.ts'

const QUIET: BroadcastPolicy = { defaultAudience: 'mentioned', allowAtAll: true, allowAtOnline: true }

type Fixture = {
  db: ReturnType<typeof createStore>
  wid: string
  cid: string
  hid: string
  a1: string
  a2: string
  sysId: string
  machineId: string
  auth: { id: string; workspaceId: string }
}

function fixture(): Fixture {
  const db = createStore(':memory:')
  const wid = newId('ws')
  db.run('INSERT INTO workspace(id, slug, name, created_at) VALUES (?,?,?,?)', wid, 'test', 'Test', now())
  const cid = newId('ch')
  db.run(
    'INSERT INTO channel(id, workspace_id, name, kind, broadcast_policy, context_scope, created_at) VALUES (?,?,?,?,?,?,?)',
    cid, wid, 'test', 'channel', JSON.stringify(QUIET), 'channel', now(),
  )
  const mkAgent = (display: string, role: 'member' | 'system' = 'member'): string => {
    const sid = newId('soul')
    db.run('INSERT INTO soul(id, workspace_id, name, kind, role, description, created_at) VALUES (?,?,?,?,?,?,?)', sid, wid, display, 'agent', role, '', now())
    const aid = newId('agt')
    db.run('INSERT INTO agent(id, workspace_id, soul_id, display_name, role, created_at) VALUES (?,?,?,?,?,?)', aid, wid, sid, display, role, now())
    db.run('INSERT INTO channel_member(channel_id, member_id, member_kind) VALUES (?,?,?)', cid, aid, 'agent')
    return aid
  }
  const a1 = mkAgent('SpecBot')
  const a2 = mkAgent('ResearcherBot')
  // D-026 system ghost author — needed by assignTask. Not a channel member (it only authors),
  // and has no instance (not a runtime target).
  const sysSoul = newId('soul')
  db.run('INSERT INTO soul(id, workspace_id, name, kind, role, description, created_at) VALUES (?,?,?,?,?,?,?)', sysSoul, wid, 'System', 'agent', 'system', '', now())
  const sysId = newId('agt')
  db.run('INSERT INTO agent(id, workspace_id, soul_id, display_name, role, created_at) VALUES (?,?,?,?,?,?)', sysId, wid, sysSoul, 'System', 'system', now())
  const hid = newId('hum')
  db.run('INSERT INTO human(id, workspace_id, name, created_at) VALUES (?,?,?,?)', hid, wid, 'Alice', now())
  db.run('INSERT INTO channel_member(channel_id, member_id, member_kind) VALUES (?,?,?)', cid, hid, 'human')

  const { machine, token } = registerMachine(db, wid, 'mac1', null)
  for (const aid of [a1, a2]) {
    upsertInstance(db, wid, aid, { machineId: machine.id, runtime: 'claude-code', online: true })
  }
  return { db, wid, cid, hid, a1, a2, sysId, machineId: machine.id, auth: authMachineByToken(db, token) }
}

const post = (f: Fixture, body: string) =>
  postMessage(f.db, f.cid, {
    body, authorId: f.hid, authorKind: 'human', threadId: null, broadcastPolicyOverride: null, contextScope: null,
  })

// set / restore an env var (delete when the saved value was undefined) for TTL-scoped tests.
function setEnv(key: string, saved: string | undefined): void {
  if (saved === undefined) delete process.env[key]
  else process.env[key] = saved
}

test('direct @mention of an online agent spawns a pending dispatch', () => {
  const f = fixture()
  const msg = post(f, '@SpecBot draft the PRD')
  const spec = msg.deliveries.find((d) => d.recipientHandle === 'SpecBot')!
  assert.equal(spec.wake, true)
  assert.equal(spec.reasonCode, 'DIRECT_MENTION')
  assert.ok(spec.dispatch, 'SpecBot wake delivery must carry a dispatch')
  assert.equal(spec.dispatch!.state, 'pending')
  const res = msg.deliveries.find((d) => d.recipientHandle === 'ResearcherBot')!
  assert.equal(res.state, 'excluded')
  assert.equal(res.dispatch, null)
  f.db.close()
})

test('quiet default (no @mention) creates ZERO dispatches — the controllable boundary', () => {
  const f = fixture()
  const msg = post(f, 'team sync: shipping v0.1')
  assert.ok(msg.deliveries.every((d) => !d.wake), 'no recipient should wake')
  assert.ok(msg.deliveries.every((d) => !d.dispatch), 'no dispatch should be created')
  f.db.close()
})

test('DEFERRED_OFFLINE creates no dispatch; offline direct-mention is QUEUED', () => {
  const f = fixture()
  upsertInstance(f.db, f.wid, f.a2, { machineId: f.machineId, runtime: 'claude-code', online: false })

  const mOnline = post(f, '@online review please')
  const res = mOnline.deliveries.find((d) => d.recipientHandle === 'ResearcherBot')!
  assert.equal(res.state, 'deferred')
  assert.equal(res.reasonCode, 'DEFERRED_OFFLINE')
  assert.equal(res.dispatch, null, 'deferred offline must not queue a dispatch')

  const mDirect = post(f, '@ResearcherBot queue this for later')
  const res2 = mDirect.deliveries.find((d) => d.recipientHandle === 'ResearcherBot')!
  assert.equal(res2.state, 'delivered')
  assert.equal(res2.wake, true)
  assert.equal(res2.dispatch!.state, 'pending', 'offline direct-mention is queued, not dropped')

  assert.ok(!pollDispatches(f.db, f.machineId).some((d) => d.agentHandle === 'ResearcherBot'),
    'queued dispatch invisible while instance offline')
  upsertInstance(f.db, f.wid, f.a2, { machineId: f.machineId, runtime: 'claude-code', online: true })
  assert.ok(pollDispatches(f.db, f.machineId).some((d) => d.agentHandle === 'ResearcherBot'),
    'queued dispatch becomes visible once an online instance exists')
  f.db.close()
})

test('claim CAS rejects double-claim; complete posts a real agent reply', () => {
  const f = fixture()
  post(f, '@SpecBot go')
  const dsp = pollDispatches(f.db, f.machineId).find((d) => d.agentHandle === 'SpecBot')!
  const claimed = claimDispatch(f.db, dsp.id, f.auth)
  assert.equal(claimed.state, 'claimed')
  assert.throws(
    () => claimDispatch(f.db, dsp.id, f.auth),
    (e) => e instanceof HttpError && e.status === 409,
  )
  const comp = completeDispatch(f.db, dsp.id, f.auth, { ok: true, replyBody: 'PRD ready @Alice' })
  assert.equal(comp.dispatch.state, 'done')
  assert.ok(comp.reply, 'complete must post the runtime reply as an agent message')
  assert.equal(comp.reply!.authorKind, 'agent')
  f.db.close()
})

test('reply re-enters the decider: @mention in a reply spawns a nested dispatch', () => {
  const f = fixture()
  post(f, '@SpecBot draft')
  const dsp = pollDispatches(f.db, f.machineId).find((d) => d.agentHandle === 'SpecBot')!
  claimDispatch(f.db, dsp.id, f.auth)
  completeDispatch(f.db, dsp.id, f.auth, { ok: true, replyBody: 'done — @ResearcherBot please review' })
  const nested = pollDispatches(f.db, f.machineId).find((d) => d.agentHandle === 'ResearcherBot')
  assert.ok(nested, 'composition: the @ResearcherBot in the reply spawned a dispatch')
  assert.equal(nested!.payload.reasonCode, 'DIRECT_MENTION')
  f.db.close()
})

test('failed completion (no reply) marks dispatch failed and posts nothing', () => {
  const f = fixture()
  post(f, '@SpecBot go')
  const dsp = pollDispatches(f.db, f.machineId).find((d) => d.agentHandle === 'SpecBot')!
  claimDispatch(f.db, dsp.id, f.auth)
  const comp = completeDispatch(f.db, dsp.id, f.auth, { ok: false, error: 'runtime blew up' })
  assert.equal(comp.dispatch.state, 'failed')
  assert.equal(comp.reply, null)
  f.db.close()
})

test('cross-workspace isolation: a foreign machine token cannot claim (404, no leak)', () => {
  const f = fixture()
  post(f, '@SpecBot go')
  const dsp = pollDispatches(f.db, f.machineId).find((d) => d.agentHandle === 'SpecBot')!
  // A real second workspace (the machine FK requires the workspace row to exist).
  const wid2 = newId('ws')
  f.db.run('INSERT INTO workspace(id, slug, name, created_at) VALUES (?,?,?,?)', wid2, 'foreign', 'Foreign', now())
  const foreign = registerMachine(f.db, wid2, 'mac-foreign', null)
  const foreignAuth = authMachineByToken(f.db, foreign.token)
  assert.throws(
    () => claimDispatch(f.db, dsp.id, foreignAuth),
    (e) => e instanceof HttpError && e.status === 404,
    'cross-workspace claim must 404 (hide existence), not 403',
  )
  f.db.close()
})

// ---------- M3: lease renewal (D-024 Directive) ----------

test('renewClaim refreshes claimed_at so an expiring lease is not requeued (no duplicate reply)', () => {
  const f = fixture()
  const TTL = 300_000
  post(f, '@SpecBot go')
  const dsp = pollDispatches(f.db, f.machineId).find((d) => d.agentHandle === 'SpecBot')!
  const claimed = claimDispatch(f.db, dsp.id, f.auth)
  assert.equal(claimed.state, 'claimed')

  // Backdate the claim so it WOULD be requeued...
  f.db.run('UPDATE dispatch SET claimed_at = ? WHERE id = ?', now() - TTL - 1000, dsp.id)
  // ...but renewing pushes claimed_at back to now (only the claimer can).
  const renewed = renewClaim(f.db, dsp.id, f.auth)
  assert.equal(renewed.state, 'claimed')
  assert.equal(renewed.claimedByMachine, f.machineId)
  assert.equal(requeueStaleClaimed(f.db, TTL), 0, 'a renewed claim must not be requeued')

  // Control: without renewing, the same stale claim IS requeued (proves the test is meaningful).
  f.db.run('UPDATE dispatch SET claimed_at = ? WHERE id = ?', now() - TTL - 1000, dsp.id)
  assert.equal(requeueStaleClaimed(f.db, TTL), 1)
  f.db.close()
})

test('renewClaim rejects non-claimer (403) and non-claimed states (409)', () => {
  const f = fixture()
  // A second machine hosting the SAME agent online → structurally eligible to claim.
  const m2 = registerMachine(f.db, f.wid, 'mac2', null)
  upsertInstance(f.db, f.wid, f.a1, { machineId: m2.machine.id, runtime: 'claude-code', online: true })
  const auth2 = authMachineByToken(f.db, m2.token)

  post(f, '@SpecBot go')
  const dsp = pollDispatches(f.db, f.machineId).find((d) => d.agentHandle === 'SpecBot')!
  claimDispatch(f.db, dsp.id, f.auth) // mac1 holds the claim

  // mac2 is NOT the claimer → 403, and mac1 keeps the lease.
  assert.throws(
    () => renewClaim(f.db, dsp.id, auth2),
    (e) => e instanceof HttpError && e.status === 403,
  )

  // pending (never claimed) → 409
  post(f, '@SpecBot pending one')
  const dsp2 = pollDispatches(f.db, f.machineId).find((d) => d.id !== dsp.id)!
  assert.throws(
    () => renewClaim(f.db, dsp2.id, f.auth),
    (e) => e instanceof HttpError && e.status === 409,
  )

  // done → 409
  completeDispatch(f.db, dsp.id, f.auth, { ok: true, replyBody: 'done' })
  assert.throws(
    () => renewClaim(f.db, dsp.id, f.auth),
    (e) => e instanceof HttpError && e.status === 409,
  )
  f.db.close()
})

// ---------- M3: instance liveness reaper (R-008 eventual consistency) ----------

test('reapStaleInstances marks a silent host offline; a polling host is never self-reaped', () => {
  const f = fixture()
  const prevC = process.env.LOOP_CLAIM_TTL_MS
  const prevI = process.env.LOOP_INSTANCE_TTL_MS
  process.env.LOOP_CLAIM_TTL_MS = '1'
  process.env.LOOP_INSTANCE_TTL_MS = '1' // clamps to claim+60s; any larger backdate is stale
  try {
    // Direct reap with a tiny TTL kills a backdated instance (no poll refresh to save it).
    f.db.run('UPDATE instance SET last_seen_at = ? WHERE machine_id = ?', now() - 10_000_000, f.machineId)
    assert.ok(reapStaleInstances(f.db, 50) >= 1, 'a silent host is reaped offline')

    // Revive, then backdate again — but this time POLL instead of reaping directly. The poll
    // refreshes the caller's online instances BEFORE reaping, so the actively-polling host
    // survives despite the huge backdate (the reference client polls, never POSTs /heartbeat).
    upsertInstance(f.db, f.wid, f.a1, { machineId: f.machineId, runtime: 'claude-code', online: true })
    f.db.run('UPDATE instance SET last_seen_at = ? WHERE machine_id = ?', now() - 10_000_000, f.machineId)
    post(f, '@SpecBot survived')
    const dispatches = pollDispatches(f.db, f.machineId)
    assert.ok(
      dispatches.some((d) => d.agentHandle === 'SpecBot'),
      'a polling host still receives its dispatch — it is not self-reaped',
    )
    const inst = f.db.get<{ online: number }>(
      'SELECT online FROM instance WHERE machine_id = ? AND agent_id = ?',
      f.machineId, f.a1,
    )
    assert.equal(inst?.online, 1, 'a polling host instance stays online')
  } finally {
    setEnv('LOOP_CLAIM_TTL_MS', prevC)
    setEnv('LOOP_INSTANCE_TTL_MS', prevI)
  }
  f.db.close()
})

test('a reaped machine\'s pending dispatch is claimable by another live machine (takeover)', () => {
  const f = fixture()
  post(f, '@SpecBot go')
  // machine1 goes silent and is reaped offline.
  f.db.run('UPDATE instance SET last_seen_at = ? WHERE machine_id = ?', now() - 10_000_000, f.machineId)
  reapStaleInstances(f.db, 50)
  // A second machine comes online hosting the same agent AFTER the reap (so it is untouched).
  const m2 = registerMachine(f.db, f.wid, 'mac2', null)
  upsertInstance(f.db, f.wid, f.a1, { machineId: m2.machine.id, runtime: 'claude-code', online: true })
  const auth2 = authMachineByToken(f.db, m2.token)

  // The dead machine no longer sees the dispatch; the live one does and can claim it.
  assert.equal(
    pollDispatches(f.db, f.machineId).filter((d) => d.agentHandle === 'SpecBot').length,
    0,
    'dead machine sees no dispatch',
  )
  const dsp = pollDispatches(f.db, m2.machine.id).find((d) => d.agentHandle === 'SpecBot')!
  assert.ok(dsp, 'live machine sees the orphaned dispatch')
  const claimed = claimDispatch(f.db, dsp.id, auth2)
  assert.equal(claimed.state, 'claimed')
  assert.equal(claimed.claimedByMachine, m2.machine.id)
  f.db.close()
})

// ---------- D-027/D-028 (V2.3): lease CAS hardening + federation liveness contract ----------
//
// These lock the subtle distributed-correctness invariants formalized in D-027/D-028.
// The CAS predicates added to complete/abandon are defense-in-depth for the planned Postgres
// swap; under node:sqlite DatabaseSync (single sync writer) the read-checks already reject,
// so these tests assert the OBSERVABLE invariant (duplicate-reply = 0) rather than the
// unreachable-under-sync CAS 0-row branch. The real Postgres-Read-Committed TOCTOU remains a
// recorded residual risk to re-verify on the actual async driver (D-027 Not-tested).

test('D-027/D-028: duplicate-reply = 0 across lease-loss/takeover (the moat surface)', () => {
  const f = fixture()
  // A second machine hosting the SAME agent online — eligible to take over the dispatch.
  const m2 = registerMachine(f.db, f.wid, 'mac2', null)
  upsertInstance(f.db, f.wid, f.a1, { machineId: m2.machine.id, runtime: 'claude-code', online: true })
  const auth2 = authMachineByToken(f.db, m2.token)

  post(f, '@SpecBot go')
  const dsp = pollDispatches(f.db, f.machineId).find((d) => d.agentHandle === 'SpecBot')!
  claimDispatch(f.db, dsp.id, f.auth) // mac1 holds the lease

  // Case 1: lease goes stale, mac2 RE-CLAIMS (still-claimed by mac2) → mac1's late complete is 403.
  f.db.run('UPDATE dispatch SET claimed_at = ? WHERE id = ?', now() - 301_000, dsp.id)
  requeueStaleClaimed(f.db, 300_000) // resets mac1's stale claim → pending
  claimDispatch(f.db, dsp.id, auth2) // mac2 takes over (state=claimed, claimed_by_machine=mac2)
  assert.throws(
    () => completeDispatch(f.db, dsp.id, f.auth, { ok: true, replyBody: 'DUP from mac1 (case1)' }),
    (e) => e instanceof HttpError && e.status === 403,
    'late complete while another machine holds the claim → 403',
  )
  // mac2 completes legitimately → exactly one agent reply lands.
  completeDispatch(f.db, dsp.id, auth2, { ok: true, replyBody: 'from mac2' })
  assert.equal(
    f.db.get<{ n: number }>("SELECT COUNT(*) AS n FROM message WHERE channel_id = ? AND author_kind = 'agent'", f.cid)!.n,
    1,
    'one agent reply so far',
  )

  // Case 2: a fresh dispatch — mac1 claims, lease requeued back to PENDING (not re-claimed)
  //         → mac1's late complete is 409 (state≠claimed), no second reply.
  post(f, '@SpecBot second')
  const dsp2 = pollDispatches(f.db, f.machineId).find((d) => d.agentHandle === 'SpecBot' && d.id !== dsp.id)!
  claimDispatch(f.db, dsp2.id, f.auth)
  f.db.run('UPDATE dispatch SET claimed_at = ? WHERE id = ?', now() - 301_000, dsp2.id)
  requeueStaleClaimed(f.db, 300_000) // → pending
  assert.equal(
    f.db.get<{ state: string }>('SELECT state FROM dispatch WHERE id = ?', dsp2.id)!.state,
    'pending',
  )
  assert.throws(
    () => completeDispatch(f.db, dsp2.id, f.auth, { ok: true, replyBody: 'DUP from mac1 (case2)' }),
    (e) => e instanceof HttpError && e.status === 409,
    'late complete on a requeued (pending) dispatch → 409',
  )

  // Net: two wakes, both late-completes rejected → still exactly ONE agent reply. Moat holds.
  assert.equal(
    f.db.get<{ n: number }>("SELECT COUNT(*) AS n FROM message WHERE channel_id = ? AND author_kind = 'agent'", f.cid)!.n,
    1,
    'no duplicate agent reply — controllable delivery holds across takeover',
  )
  f.db.close()
})

test('D-028: reaper is global — machine B\'s poll reaps silent machine A\'s instance', () => {
  const f = fixture()
  const m2 = registerMachine(f.db, f.wid, 'mac2', null)
  upsertInstance(f.db, f.wid, f.a2, { machineId: m2.machine.id, runtime: 'claude-code', online: true })

  // machine A (mac1) goes silent — backdate its instance far past any TTL.
  f.db.run('UPDATE instance SET last_seen_at = ? WHERE machine_id = ?', now() - 10_000_000, f.machineId)
  assert.equal(
    f.db.get<{ online: number }>('SELECT online FROM instance WHERE machine_id = ? AND agent_id = ?', f.machineId, f.a1)!.online,
    1,
  )
  // machine B polls — pollDispatches runs reapStaleInstances GLOBALLY (not scoped to the caller),
  // so A's silent instance is reaped even though B issued the poll.
  pollDispatches(f.db, m2.machine.id)
  assert.equal(
    f.db.get<{ online: number }>('SELECT online FROM instance WHERE machine_id = ? AND agent_id = ?', f.machineId, f.a1)!.online,
    0,
    "B's poll reaped A's silent instance — the reaper is not isolated per machine",
  )
  f.db.close()
})

test('D-028: two machines claiming the same pending dispatch resolve to one winner (federation CAS)', () => {
  const f = fixture()
  const m2 = registerMachine(f.db, f.wid, 'mac2', null)
  upsertInstance(f.db, f.wid, f.a1, { machineId: m2.machine.id, runtime: 'claude-code', online: true })
  const auth2 = authMachineByToken(f.db, m2.token)

  post(f, '@SpecBot go')
  const dsp = pollDispatches(f.db, f.machineId).find((d) => d.agentHandle === 'SpecBot')!
  // mac1 wins the pending→claimed CAS.
  const claimed = claimDispatch(f.db, dsp.id, f.auth)
  assert.equal(claimed.state, 'claimed')
  assert.equal(claimed.claimedByMachine, f.machineId)
  // mac2 is rejected — the WHERE state='pending' CAS yielded zero rows. No double-claim.
  assert.throws(
    () => claimDispatch(f.db, dsp.id, auth2),
    (e) => e instanceof HttpError && e.status === 409,
  )
  f.db.close()
})

test('D-027: TTL invariants — instanceTtlMs > claimTtlMs across default / override / clamp', () => {
  const prevC = process.env.LOOP_CLAIM_TTL_MS
  const prevI = process.env.LOOP_INSTANCE_TTL_MS
  const cases: Array<{ name: string; claim?: string; inst?: string }> = [
    { name: 'defaults' },
    { name: 'claim override 120s', claim: '120000' },
    { name: 'instance override 20min', inst: '1200000' },
    { name: 'instance < claim triggers clamp', claim: '300000', inst: '60000' },
  ]
  try {
    for (const c of cases) {
      setEnv('LOOP_CLAIM_TTL_MS', prevC)
      setEnv('LOOP_INSTANCE_TTL_MS', prevI)
      if (c.claim !== undefined) process.env.LOOP_CLAIM_TTL_MS = c.claim
      if (c.inst !== undefined) process.env.LOOP_INSTANCE_TTL_MS = c.inst
      const claim = claimTtlMs()
      const inst = instanceTtlMs()
      assert.ok(inst > claim, `${c.name}: instanceTtl(${inst}) must be > claimTtl(${claim})`)
      if (c.name.startsWith('instance < claim')) {
        assert.equal(inst, claim + 60_000, 'clamp path must return claim+60s')
      }
    }
  } finally {
    setEnv('LOOP_CLAIM_TTL_MS', prevC)
    setEnv('LOOP_INSTANCE_TTL_MS', prevI)
  }
})

// ---------- D-026: task<->dispatch lifecycle coupling + cancel ----------

// 6. claimDispatch auto-couples task open->in_progress (idempotent; re-claim leaves it in_progress).
test('D-026 claim coupling: claiming an assignment dispatch advances task open->in_progress', () => {
  const f = fixture()
  const task = createTask(f.db, f.wid, { title: 'do work' })
  assignTask(f.db, f.wid, task.id, { assigneeId: f.a1, channelId: f.cid })
  assert.equal(getTask(f.db, f.wid, task.id).status, 'open', 'task is open until a claim')

  const dsp = pollDispatches(f.db, f.machineId).find((d) => d.taskId === task.id)!
  claimDispatch(f.db, dsp.id, f.auth)
  assert.equal(getTask(f.db, f.wid, task.id).status, 'in_progress')

  // Abandon re-claims: re-claiming is idempotent — it stays in_progress (the open->in_progress
  // UPDATE matches zero rows the second time, which is fine).
  abandonDispatch(f.db, dsp.id, f.auth) // back to pending
  claimDispatch(f.db, dsp.id, f.auth) // re-claim
  assert.equal(getTask(f.db, f.wid, task.id).status, 'in_progress', 're-claim keeps task in_progress')
  f.db.close()
})

// 7. completeDispatch with ok -> task in_progress->done; completeDispatch with ok=false (failed)
//    -> task stays in_progress.
test('D-026 complete coupling: ok advances in_progress->done; failed leaves it in_progress', () => {
  const f = fixture()
  // ok path: done.
  const task = createTask(f.db, f.wid, { title: 'success' })
  assignTask(f.db, f.wid, task.id, { assigneeId: f.a1, channelId: f.cid })
  const dsp = pollDispatches(f.db, f.machineId).find((d) => d.taskId === task.id)!
  claimDispatch(f.db, dsp.id, f.auth)
  completeDispatch(f.db, dsp.id, f.auth, { ok: true, replyBody: 'done' })
  assert.equal(getTask(f.db, f.wid, task.id).status, 'done')

  // failed path: a separate task stays in_progress (one runtime failure must not close it).
  const task2 = createTask(f.db, f.wid, { title: 'failure' })
  assignTask(f.db, f.wid, task2.id, { assigneeId: f.a2, channelId: f.cid })
  const dsp2 = pollDispatches(f.db, f.machineId).find((d) => d.taskId === task2.id)!
  claimDispatch(f.db, dsp2.id, f.auth)
  completeDispatch(f.db, dsp2.id, f.auth, { ok: false, error: 'boom' })
  assert.equal(getTask(f.db, f.wid, task2.id).status, 'in_progress', 'failed complete keeps task in_progress')
  f.db.close()
})

// 8. cancelTask -> task cancelled AND every related non-terminal dispatch becomes state dead;
//    a late completeDispatch on a dead dispatch -> 409 and NO agent reply posted (duplicate-reply = 0).
test('D-026 cancelTask: kills related dispatches; late complete is 409 with no duplicate reply', () => {
  const f = fixture()
  const task = createTask(f.db, f.wid, { title: 'abort' })
  assignTask(f.db, f.wid, task.id, { assigneeId: f.a1, channelId: f.cid })
  const dsp = pollDispatches(f.db, f.machineId).find((d) => d.taskId === task.id)!
  claimDispatch(f.db, dsp.id, f.auth)

  cancelTask(f.db, f.wid, task.id)
  assert.equal(getTask(f.db, f.wid, task.id).status, 'cancelled')
  const dead = f.db.get<{ state: string }>('SELECT state FROM dispatch WHERE id = ?', dsp.id)!
  assert.equal(dead.state, 'dead', 'non-terminal dispatch becomes dead on cancel')

  // A late complete on the dead dispatch -> 409 (state!=='claimed'), and NO agent reply.
  const before = f.db.get<{ n: number }>(
    "SELECT COUNT(*) AS n FROM message WHERE channel_id = ? AND author_kind = 'agent'",
    f.cid,
  )!.n
  assert.throws(
    () => completeDispatch(f.db, dsp.id, f.auth, { ok: true, replyBody: 'late reply should not land' }),
    (e) => e instanceof HttpError && e.status === 409,
    'late complete on a dead dispatch must be 409',
  )
  const after = f.db.get<{ n: number }>(
    "SELECT COUNT(*) AS n FROM message WHERE channel_id = ? AND author_kind = 'agent'",
    f.cid,
  )!.n
  assert.equal(after, before, 'no duplicate agent reply from a late complete on a cancelled task')
  f.db.close()
})

// 10. hard-ban: the agent routing/wake/lifecycle paths contain zero strings.Contains / keyword
//     tables. Fails on forbidden patterns (.contains/.indexOf/substring routing) outside the
//     single allowed carve-out (delivery/parser.ts @mention tokenization).
test('D-026 hard-ban: no new body-substring routing (no .contains/.indexOf keyword tables) outside parser.ts', () => {
  const here = dirname(fileURLToPath(import.meta.url))
  const srcDir = join(here, '..')
  const files = [
    'api/delivery-service.ts',
    'api/dispatch-service.ts',
    'api/task-service.ts',
    'api/routes.ts',
    'delivery/decider.ts',
    'delivery/types.ts',
    'seed/pm-scenario.ts',
  ]
  // Forbids substring routing on message bodies / agent decisions. The decider already forbids
  // body inspection by construction; this guards against regressions that reintroduce keyword
  // matching. parser.ts (the documented @mention tokenizer carve-out) is intentionally excluded.
  const FORBIDDEN = /\.contains\s*\(|\.indexOf\s*\(|\.includes\s*\(['"]/i
  for (const rel of files) {
    const src = readFileSync(join(srcDir, rel), 'utf8')
    const hits = src.split('\n').reduce<string[]>((acc, line, i) => {
      if (FORBIDDEN.test(line)) acc.push(`${rel}:${i + 1}: ${line.trim()}`)
      return acc
    }, [])
    assert.equal(
      hits.length,
      0,
      `${rel} must not use substring/keyword routing (hard-ban). Hits:\n${hits.join('\n')}`,
    )
  }
})
