// Dispatch service — the runtime execution bridge (D-024).
//
// The control plane NEVER executes a runtime (D-021). When a message wakes an agent,
// delivery-service records a `dispatch` targeting that agent (the PRODUCE side). THIS
// file is the CONSUME side: a Machine hosting an ONLINE instance of the agent polls,
// claims, and completes the dispatch. On `complete` with a replyBody, the runtime's
// output is posted back as an agent-authored message via the same postMessage critical
// path — so the reply re-enters the decider and its delivery diagnostics compose with
// the original wake. Every step is auditable in the dispatch lifecycle.
//
// Eligibility (who can claim) is a STRUCTURED SQL fact — "this machine has an online
// instance of the dispatch's agent" — never string/keyword matching. Token auth is
// sha256-over-the-bearer credential lookup (standard API-key pattern), not behavioral
// routing. CLAUDE.md agent/LLM hard-ban is honored.

import { createHash } from 'node:crypto'

import type { Db } from '../db/client.ts'
import { newId, now } from '../db/id.ts'
import { postMessage } from './delivery-service.ts'
import type {
  DispatchResultView,
  DispatchState,
  DispatchView,
  InstanceView,
  MachineView,
  MessageView,
} from './contract.ts'
import {
  dispatchToView,
  instanceView,
  machineView,
  type DispatchPayload,
  type RawDispatch,
} from './views.ts'

// HTTP status union kept to literals Hono's c.json accepts verbatim.
export class HttpError extends Error {
  readonly status: 400 | 401 | 403 | 404 | 409
  constructor(status: 400 | 401 | 403 | 404 | 409, message: string) {
    super(message)
    this.name = 'HttpError'
    this.status = status
  }
}

const DISPATCH_COLS =
  'id, message_id, delivery_id, task_id, channel_id, thread_id, agent_id, runtime, state, payload, result, claimed_by_machine, claimed_at, completed_at, created_at'

export function hashToken(token: string): string {
  return createHash('sha256').update(token).digest('hex')
}

// ---------- machine + instance setup ----------

export function registerMachine(
  db: Db,
  workspaceId: string,
  name: string,
  owner: string | null,
): { machine: MachineView; token: string } {
  const existing = db.get<{ id: string }>(
    'SELECT id FROM machine WHERE workspace_id = ? AND name = ?',
    workspaceId,
    name,
  )
  if (existing) throw new HttpError(409, `machine '${name}' already exists`)
  const id = newId('mch')
  const token = newId('mch') // opaque bearer; returned ONCE, stored only as sha256
  db.run(
    'INSERT INTO machine(id, workspace_id, name, owner, token_hash, token_suffix, created_at) VALUES (?,?,?,?,?,?,?)',
    id,
    workspaceId,
    name,
    owner,
    hashToken(token),
    token.slice(-4),
    now(),
  )
  return { machine: machineView(db, id), token }
}

export function upsertInstance(
  db: Db,
  workspaceId: string,
  agentId: string,
  input: { machineId: string; runtime: string; online: boolean },
): InstanceView {
  const agent = db.get<{ id: string }>(
    'SELECT id FROM agent WHERE id = ? AND workspace_id = ?',
    agentId,
    workspaceId,
  )
  if (!agent) throw new HttpError(404, 'agent not found')
  const machine = db.get<{ id: string }>(
    'SELECT id FROM machine WHERE id = ? AND workspace_id = ?',
    input.machineId,
    workspaceId,
  )
  if (!machine) throw new HttpError(404, 'machine not found')

  const ts = now()
  const existing = db.get<{ id: string }>(
    'SELECT id FROM instance WHERE agent_id = ? AND machine_id = ? AND runtime = ?',
    agentId,
    input.machineId,
    input.runtime,
  )
  let id: string
  if (existing) {
    id = existing.id
    db.run(
      'UPDATE instance SET online = ?, last_seen_at = ? WHERE id = ?',
      input.online ? 1 : 0,
      ts,
      id,
    )
  } else {
    id = newId('inst')
    db.run(
      'INSERT INTO instance(id, agent_id, machine_id, runtime, online, last_seen_at, created_at) VALUES (?,?,?,?,?,?,?)',
      id,
      agentId,
      input.machineId,
      input.runtime,
      input.online ? 1 : 0,
      ts,
      ts,
    )
  }
  return instanceView(db, id)
}

export function heartbeat(db: Db, machineId: string): { onlineInstances: number } {
  const ts = now()
  db.run(
    'UPDATE instance SET last_seen_at = ? WHERE machine_id = ? AND online = 1',
    ts,
    machineId,
  )
  const r = db.get<{ n: number }>(
    'SELECT COUNT(*) AS n FROM instance WHERE machine_id = ? AND online = 1',
    machineId,
  )
  return { onlineInstances: r?.n ?? 0 }
}

// ---------- auth ----------

export type AuthedMachine = { id: string; workspaceId: string }

export function authMachineByToken(db: Db, bearer: string | undefined): AuthedMachine {
  if (!bearer) throw new HttpError(401, 'missing bearer token')
  const row = db.get<{ id: string; workspace_id: string }>(
    'SELECT id, workspace_id FROM machine WHERE token_hash = ?',
    hashToken(bearer),
  )
  if (!row) throw new HttpError(401, 'invalid token')
  return { id: row.id, workspaceId: row.workspace_id }
}

// ---------- dispatch consume ----------

function machineEligible(db: Db, machineId: string, agentId: string): boolean {
  const r = db.get<{ n: number }>(
    'SELECT COUNT(*) AS n FROM instance WHERE machine_id = ? AND agent_id = ? AND online = 1',
    machineId,
    agentId,
  )
  return (r?.n ?? 0) > 0
}

// Requeue dispatches a machine claimed but never completed (crash / network loss). The
// lease is generous (5 min default) because real external runtimes (claude -p etc.) can
// take minutes; tune via LOOP_CLAIM_TTL_MS. A proper lease-renewal (heartbeat-during-exec)
// is deferred to M3 — until then, a runtime exceeding the TTL will have its dispatch
// requeued (review finding: avoid duplicate replies by keeping the TTL above runtime latency).
export function claimTtlMs(): number {
  const env = Number(process.env.LOOP_CLAIM_TTL_MS)
  return Number.isFinite(env) && env > 0 ? env : 300_000
}

export function requeueStaleClaimed(db: Db, ttlMs: number = claimTtlMs()): number {
  const cutoff = now() - ttlMs
  const r = db.run(
    "UPDATE dispatch SET state = 'pending', claimed_by_machine = NULL, claimed_at = NULL WHERE state = 'claimed' AND claimed_at < ?",
    cutoff,
  )
  return r.changes
}

// Instance liveness TTL — how long an instance stays "online" after its host's last
// heartbeat/poll. MUST be greater than LOOP_CLAIM_TTL_MS (and greater than a single
// real-runtime exec): the reference machine client polls but does not POST /heartbeat,
// so a poll refreshes last_seen_at (see pollDispatches) and only a truly-silent host
// (crash / network loss) lets its instances cross this threshold. R-008 eventual-consistency
// closure: a reaped-offline instance's pending dispatches are no longer eligible for that
// machine (the EXISTS online=1 filter) and wait for another machine hosting the agent.
export function instanceTtlMs(): number {
  const claim = claimTtlMs()
  const raw = Number(process.env.LOOP_INSTANCE_TTL_MS)
  if (Number.isFinite(raw) && raw > 0) {
    if (raw < claim) {
      console.warn(
        `[loop] LOOP_INSTANCE_TTL_MS=${raw}ms < LOOP_CLAIM_TTL_MS=${claim}ms; clamping to claim+60s to avoid reaping instances mid-exec.`,
      )
      return claim + 60_000
    }
    return raw
  }
  return Math.max(claim * 2, 600_000)
}

// Mark instances whose host has gone silent offline. Pure structured fact
// (`online = 1 AND last_seen_at < cutoff`) — no string/keyword routing (hard-ban honored).
export function reapStaleInstances(db: Db, ttlMs: number = instanceTtlMs()): number {
  const cutoff = now() - ttlMs
  const r = db.run(
    'UPDATE instance SET online = 0 WHERE online = 1 AND last_seen_at IS NOT NULL AND last_seen_at < ?',
    cutoff,
  )
  return r.changes
}

// Pending dispatches for agents this machine can run (online instance present). A poll is
// also a liveness signal: we refresh THIS machine's online instances' last_seen_at first
// (reusing heartbeat), so an actively-polling host is never reaped; then reap silent hosts'
// instances and requeue expired leases, then read. All under one transaction — node:sqlite
// is synchronous so each statement's effect is visible to the next; the tx keeps reap/requeue
// atomic vs a concurrent cross-process claim if this ever swaps to an async driver (client.ts
// notes Postgres as the swap point).
//
// D-028 contract (federation liveness/reaper/takeover, closes R-008): the
// refresh→reap→requeue→read ordering above is the invariant set — (a) poll=liveness
// (self-reap unreachable because refresh precedes reap in one tx), (b) instanceTtl≥claimTtl,
// (c) takeover = stale-claim reset + pending→claimed CAS (requeue is pure claim-TTL,
// decoupled from instance reap). The atomicity is a property of node:sqlite DatabaseSync's
// single sync writer, NOT a SQL isolation level — any async driver / connection-pool /
// Postgres swap MUST re-establish no-interleaving (SELECT FOR UPDATE / advisory lock) first.
export function pollDispatches(db: Db, machineId: string, limit = 16): DispatchView[] {
  return db.transaction(() => {
    heartbeat(db, machineId)
    reapStaleInstances(db)
    requeueStaleClaimed(db)
    const rows = db.all<RawDispatch>(
      `SELECT ${DISPATCH_COLS} FROM dispatch
       WHERE state = 'pending'
         AND EXISTS (SELECT 1 FROM instance i WHERE i.agent_id = dispatch.agent_id AND i.machine_id = ? AND i.online = 1)
       ORDER BY created_at
       LIMIT ?`,
      machineId,
      limit,
    )
    return rows.map((r) => dispatchToView(db, r))
  })
}

type RawDispatchScoped = RawDispatch & { workspace_id: string }

function loadScoped(db: Db, dispatchId: string): RawDispatchScoped {
  const d = db.get<RawDispatchScoped>(
    `SELECT ${DISPATCH_COLS}, workspace_id FROM dispatch WHERE id = ?`,
    dispatchId,
  )
  if (!d) throw new HttpError(404, 'dispatch not found')
  return d
}

// Atomic pending -> claimed, but only if this machine is eligible. The CAS
// (`WHERE id = ? AND state = 'pending'`) makes concurrent claims from two machines
// resolve to exactly one winner (changes === 1).
export function claimDispatch(db: Db, dispatchId: string, machine: AuthedMachine): DispatchView {
  return db.transaction(() => {
    const d = loadScoped(db, dispatchId)
    if (d.workspace_id !== machine.workspaceId) throw new HttpError(404, 'dispatch not found')
    if (d.state !== 'pending') throw new HttpError(409, `dispatch already ${d.state}`)
    if (!machineEligible(db, machine.id, d.agent_id)) {
      throw new HttpError(403, 'machine has no online instance of this agent')
    }
    const r = db.run(
      "UPDATE dispatch SET state = 'claimed', claimed_by_machine = ?, claimed_at = ? WHERE id = ? AND state = 'pending'",
      machine.id,
      now(),
      dispatchId,
    )
    if (r.changes !== 1) throw new HttpError(409, 'dispatch was claimed by another machine')
    // D-026 lifecycle coupling: a claimed dispatch anchored to a task advances the task
    // open -> in_progress. Idempotent (0 rows is fine — the task may already be in_progress
    // from a prior claim cycle or be in another state). Best-effort, inside the claim tx.
    if (d.task_id) {
      db.run(
        "UPDATE task SET status = 'in_progress' WHERE id = ? AND status = 'open'",
        d.task_id,
      )
    }
    return dispatchToView(db, db.get<RawDispatch>(`SELECT ${DISPATCH_COLS} FROM dispatch WHERE id = ?`, dispatchId)!)
  })
}

// Claimed -> done|failed. On ok + replyBody, the runtime's output is posted back as an
// agent message through the SAME postMessage path (so the reply gets its own delivery
// diagnostics, and any @mention in the reply spawns further dispatches — composable).
export function completeDispatch(
  db: Db,
  dispatchId: string,
  machine: AuthedMachine,
  input: { ok: boolean; replyBody?: string; error?: string },
): { dispatch: DispatchView; reply: MessageView | null } {
  return db.transaction(() => {
    const d = loadScoped(db, dispatchId)
    if (d.workspace_id !== machine.workspaceId) throw new HttpError(404, 'dispatch not found')
    if (d.state !== 'claimed') throw new HttpError(409, `dispatch not claimed (state=${d.state})`)
    if (d.claimed_by_machine !== machine.id) {
      throw new HttpError(403, 'dispatch claimed by another machine')
    }

    const result: DispatchResultView = {
      ok: input.ok,
      ...(input.replyBody ? { replyBody: input.replyBody } : {}),
      ...(input.error ? { error: input.error } : {}),
    }
    // D-027: CAS-predicated UPDATE (defense-in-depth for the planned Postgres swap).
    // The read-checks above throw precise 403/409 diagnostics; this WHERE clause +
    // changes===1 assertion close the read-after-write TOCTOU window that opens under an
    // async driver / Postgres Read Committed, where two machines' complete transactions
    // could interleave between the read and an unconditional UPDATE — both would hit, and
    // postMessage below would run twice → a real duplicate agent reply (the only true
    // duplicate-reply surface). Under node:sqlite DatabaseSync (single sync writer) this
    // UPDATE never 0-rows when the read-checks passed; the assertion is belt-and-braces.
    const upd = db.run(
      "UPDATE dispatch SET state = ?, result = ?, completed_at = ? WHERE id = ? AND state = 'claimed' AND claimed_by_machine = ?",
      input.ok ? 'done' : 'failed',
      JSON.stringify(result),
      now(),
      dispatchId,
      machine.id,
    )
    if (upd.changes !== 1) throw new HttpError(409, 'dispatch lease lost (concurrent complete)')

    // D-026 lifecycle coupling: a successful (ok) completion anchored to a task advances the
    // task in_progress -> done. Idempotent (0 rows is fine — task may be done already, or in
    // a terminal state from cancel). On failure (ok=false) we do NOT touch the task: one
    // runtime failure must not close the task — it stays in_progress for a retry/abandon.
    if (input.ok && d.task_id) {
      const taskUpd = db.run(
        "UPDATE task SET status = 'done' WHERE id = ? AND status = 'in_progress'",
        d.task_id,
      )
      if (taskUpd.changes === 0) {
        // Surface a best-effort notice (not an error): the dispatch completed ok, but the task
        // had already moved (done/cancelled) — lets the panel explain a done-dispatch whose
        // task did not advance here.
        result.notice = 'task already moved out of in_progress; not advanced by this complete'
      }
    }

    let reply: MessageView | null = null
    if (input.ok && input.replyBody && input.replyBody.trim()) {
      const payload = JSON.parse(d.payload) as DispatchPayload
      reply = postMessage(db, d.channel_id, {
        body: input.replyBody,
        authorId: d.agent_id,
        authorKind: 'agent',
        threadId: d.thread_id,
        broadcastPolicyOverride: null,
        // Re-enter the SAME scope the waking message had (captured at dispatch creation),
        // so e.g. a thread dispatch's reply stays in the thread, and a channel reply does
        // not accidentally inherit a private/thread channel scope (review finding).
        contextScope: payload.contextScope,
      })
    }
    return {
      dispatch: dispatchToView(db, db.get<RawDispatch>(`SELECT ${DISPATCH_COLS} FROM dispatch WHERE id = ?`, dispatchId)!),
      reply,
    }
  })
}

// Release a claim back to pending (the runtime gave up / wants to retry later).
export function abandonDispatch(db: Db, dispatchId: string, machine: AuthedMachine): DispatchView {
  return db.transaction(() => {
    const d = loadScoped(db, dispatchId)
    if (d.workspace_id !== machine.workspaceId) throw new HttpError(404, 'dispatch not found')
    if (d.state !== 'claimed') throw new HttpError(409, `dispatch not claimed (state=${d.state})`)
    if (d.claimed_by_machine !== machine.id) {
      throw new HttpError(403, 'dispatch claimed by another machine')
    }
    // D-027: CAS-predicated UPDATE mirroring renewClaim/completeDispatch — closes the
    // read-after-write TOCTOU window under an async driver / Postgres swap (see completeDispatch).
    const upd = db.run(
      "UPDATE dispatch SET state = 'pending', claimed_by_machine = NULL, claimed_at = NULL WHERE id = ? AND state = 'claimed' AND claimed_by_machine = ?",
      dispatchId,
      machine.id,
    )
    if (upd.changes !== 1) throw new HttpError(409, 'dispatch lease lost (concurrent abandon)')
    return dispatchToView(db, db.get<RawDispatch>(`SELECT ${DISPATCH_COLS} FROM dispatch WHERE id = ?`, dispatchId)!)
  })
}

// Claimed lease renewal (M3, D-024 Directive). A machine executing a long runtime
// (claude -p etc., potentially minutes) renews the lease periodically so that
// requeueStaleClaimed does not reset its claim mid-exec — which would let another
// machine re-claim the same dispatch and produce a DUPLICATE agent reply. CAS mirrors
// abandon/complete: only the current claimer, only while still claimed. If the lease was
// already lost (requeued / re-claimed by another machine) the CAS matches zero rows and
// the caller treats the 409 as "gracefully abandon exec" — no duplicate reply, no moat break.
export function renewClaim(db: Db, dispatchId: string, machine: AuthedMachine): DispatchView {
  return db.transaction(() => {
    const d = loadScoped(db, dispatchId)
    if (d.workspace_id !== machine.workspaceId) throw new HttpError(404, 'dispatch not found')
    if (d.state !== 'claimed') throw new HttpError(409, `dispatch not claimed (state=${d.state})`)
    if (d.claimed_by_machine !== machine.id) {
      throw new HttpError(403, 'dispatch claimed by another machine')
    }
    const r = db.run(
      "UPDATE dispatch SET claimed_at = ? WHERE id = ? AND state = 'claimed' AND claimed_by_machine = ?",
      now(),
      dispatchId,
      machine.id,
    )
    if (r.changes !== 1) throw new HttpError(409, 'dispatch lease lost')
    return dispatchToView(db, db.get<RawDispatch>(`SELECT ${DISPATCH_COLS} FROM dispatch WHERE id = ?`, dispatchId)!)
  })
}

// State helper for any future out-of-band reaper / dead-letter tooling.
export function dispatchState(db: Db, dispatchId: string): DispatchState | undefined {
  const r = db.get<{ state: DispatchState }>('SELECT state FROM dispatch WHERE id = ?', dispatchId)
  return r?.state
}
