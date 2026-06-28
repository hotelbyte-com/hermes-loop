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
  'id, message_id, delivery_id, channel_id, thread_id, agent_id, runtime, state, payload, result, claimed_by_machine, claimed_at, completed_at, created_at'

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
function claimTtlMs(): number {
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

// Pending dispatches for agents this machine can run (online instance present).
export function pollDispatches(db: Db, machineId: string, limit = 16): DispatchView[] {
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
    db.run(
      "UPDATE dispatch SET state = ?, result = ?, completed_at = ? WHERE id = ?",
      input.ok ? 'done' : 'failed',
      JSON.stringify(result),
      now(),
      dispatchId,
    )

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
    db.run(
      "UPDATE dispatch SET state = 'pending', claimed_by_machine = NULL, claimed_at = NULL WHERE id = ?",
      dispatchId,
    )
    return dispatchToView(db, db.get<RawDispatch>(`SELECT ${DISPATCH_COLS} FROM dispatch WHERE id = ?`, dispatchId)!)
  })
}

// State helper for any future out-of-band reaper / dead-letter tooling.
export function dispatchState(db: Db, dispatchId: string): DispatchState | undefined {
  const r = db.get<{ state: DispatchState }>('SELECT state FROM dispatch WHERE id = ?', dispatchId)
  return r?.state
}
