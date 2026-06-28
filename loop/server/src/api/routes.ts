// Hono HTTP routes for the Loop control plane.
//
// The critical path POST /api/channels/:cid/messages delegates to
// api/delivery-service.ts -> delivery/decider.ts (the moat). This file is HTTP only.

import { Hono, type Context } from 'hono'

import type { Db } from '../db/client.ts'
import { newId, now } from '../db/id.ts'
import type { BroadcastPolicy, ContextScope, MemberKind } from '../delivery/types.ts'
import { postMessage } from './delivery-service.ts'
import {
  abandonDispatch,
  authMachineByToken,
  claimDispatch,
  completeDispatch,
  heartbeat,
  HttpError,
  pollDispatches,
  registerMachine,
  upsertInstance,
} from './dispatch-service.ts'
import {
  broadcastPolicySchema,
  type AgentView,
  type ChannelView,
  type HumanView,
  type MachineView,
  type WorkspaceView,
  zAddMember,
  zCreateChannel,
  zCreateThread,
  zCreateWorkspace,
  zPostMessage,
  zRegisterMachine,
  zUpsertInstance,
  zCompleteDispatch,
} from './contract.ts'
import {
  channelView,
  countMembers,
  enrichMessage,
  handleOf,
  loadDeliveriesForMessages,
  workspaceView,
  type RawMessage,
} from './views.ts'
import { seedPmScenario } from '../seed/pm-scenario.ts'

function bad(c: Context, error: string) {
  return c.json({ error }, 400)
}

function notFound(c: Context, error: string) {
  return c.json({ error }, 404)
}

// Map a service-layer HttpError to its literal status (Hono's c.json accepts these verbatim).
// Non-HttpError throws fall through to app.onError (500).
function errResponse(c: Context, e: unknown): Response {
  if (e instanceof HttpError) {
    const s = e.status
    if (s === 400) return c.json({ error: e.message }, 400)
    if (s === 401) return c.json({ error: e.message }, 401)
    if (s === 403) return c.json({ error: e.message }, 403)
    if (s === 404) return c.json({ error: e.message }, 404)
    return c.json({ error: e.message }, 409) // only 409 remains in the union
  }
  throw e
}

// Authorization: Bearer <token> scheme parsing (HTTP protocol), not message/behavior routing.
function bearerOf(c: Context): string | undefined {
  const h = c.req.header('authorization')
  if (!h) return undefined
  const m = /^Bearer\s+(.+)$/i.exec(h)
  return m ? m[1].trim() : undefined
}

export function buildApp(db: Db): Hono {
  const app = new Hono()

  // Never leak stack traces / raw throw bodies as 500s (review finding).
  app.onError((err, c) => {
    console.error('[loop] unhandled request error:', err)
    return c.json({ error: 'internal error' }, 500)
  })

  app.get('/api/health', (c) => c.json({ ok: true, ts: now() }))

  // ---------- workspaces ----------

  app.post('/api/workspaces', async (c) => {
    const parsed = zCreateWorkspace.safeParse(await c.req.json().catch(() => null))
    if (!parsed.success) return bad(c, parsed.error.message)
    const { slug, name } = parsed.data
    const existing = db.get<{ id: string }>('SELECT id FROM workspace WHERE slug = ?', slug)
    if (existing) return c.json(workspaceView(db, existing.id))
    const id = newId('ws')
    db.run('INSERT INTO workspace(id, slug, name, created_at) VALUES (?,?,?,?)', id, slug, name, now())
    return c.json(workspaceView(db, id), 201)
  })

  app.get('/api/workspaces', (c) => {
    const rows = db.all<{ id: string; slug: string; name: string; created_at: number }>(
      'SELECT id, slug, name, created_at FROM workspace ORDER BY created_at',
    )
    return c.json(rows.map((r) => ({ id: r.id, slug: r.slug, name: r.name, createdAt: r.created_at })))
  })

  app.get('/api/workspaces/:wid', (c) => {
    const ws = db.get<{ id: string; slug: string; name: string; created_at: number }>(
      'SELECT id, slug, name, created_at FROM workspace WHERE id = ?',
      c.req.param('wid'),
    )
    if (!ws) return notFound(c, 'workspace not found')
    return c.json({ id: ws.id, slug: ws.slug, name: ws.name, createdAt: ws.created_at })
  })

  // ---------- channels ----------

  app.get('/api/workspaces/:wid/channels', (c) => {
    const rows = db.all<{
      id: string
      workspace_id: string
      name: string
      kind: string
      broadcast_policy: string
      context_scope: string
      created_at: number
    }>(
      'SELECT id, workspace_id, name, kind, broadcast_policy, context_scope, created_at FROM channel WHERE workspace_id = ? ORDER BY created_at',
      c.req.param('wid'),
    )
    const views: ChannelView[] = rows.map((r) => ({
      id: r.id,
      workspaceId: r.workspace_id,
      name: r.name,
      kind: r.kind,
      broadcastPolicy: JSON.parse(r.broadcast_policy) as BroadcastPolicy,
      contextScope: r.context_scope as ContextScope,
      memberCount: countMembers(db, r.id),
      createdAt: r.created_at,
    }))
    return c.json(views)
  })

  app.post('/api/workspaces/:wid/channels', async (c) => {
    const wid = c.req.param('wid')
    const ws = db.get<{ id: string }>('SELECT id FROM workspace WHERE id = ?', wid)
    if (!ws) return notFound(c, 'workspace not found')
    const parsed = zCreateChannel.safeParse(await c.req.json().catch(() => null))
    if (!parsed.success) return bad(c, parsed.error.message)
    const { name, kind = 'channel', broadcastPolicy, contextScope = 'channel' } = parsed.data
    const existing = db.get<{ id: string }>(
      'SELECT id FROM channel WHERE workspace_id = ? AND name = ?',
      wid, name,
    )
    if (existing) return c.json(channelView(db, existing.id))
    const policy =
      broadcastPolicy ?? ({ defaultAudience: 'mentioned', allowAtAll: true, allowAtOnline: true } as BroadcastPolicy)
    const id = newId('ch')
    db.run(
      'INSERT INTO channel(id, workspace_id, name, kind, broadcast_policy, context_scope, created_at) VALUES (?,?,?,?,?,?,?)',
      id, wid, name, kind, JSON.stringify(policy), contextScope, now(),
    )
    return c.json(channelView(db, id), 201)
  })

  // ---------- roster ----------

  app.get('/api/workspaces/:wid/agents', (c) => {
    const rows = db.all<{
      id: string
      display_name: string
      role: string | null
      description: string | null
      soul_name: string
      n: number
      runtime: string | null
    }>(
      `SELECT a.id, a.display_name, s.role, s.description, s.name AS soul_name,
              (SELECT COUNT(*) FROM instance i WHERE i.agent_id = a.id AND i.online = 1) AS n,
              (SELECT i.runtime FROM instance i WHERE i.agent_id = a.id ORDER BY i.online DESC LIMIT 1) AS runtime
       FROM agent a JOIN soul s ON s.id = a.soul_id
       WHERE a.workspace_id = ? ORDER BY a.display_name`,
      c.req.param('wid'),
    )
    const views: AgentView[] = rows.map((r) => ({
      id: r.id,
      displayName: r.display_name,
      soulName: r.soul_name,
      role: r.role,
      description: r.description,
      online: r.n > 0,
      runtime: r.runtime,
    }))
    return c.json(views)
  })

  app.get('/api/workspaces/:wid/humans', (c) => {
    const rows = db.all<{ id: string; name: string }>(
      'SELECT id, name FROM human WHERE workspace_id = ? ORDER BY name',
      c.req.param('wid'),
    )
    return c.json(rows as HumanView[])
  })

  app.get('/api/workspaces/:wid/members', (c) => {
    const agents = db.all<{ id: string; display_name: string }>(
      'SELECT id, display_name FROM agent WHERE workspace_id = ?',
      c.req.param('wid'),
    )
    const humans = db.all<{ id: string; name: string }>(
      'SELECT id, name FROM human WHERE workspace_id = ?',
      c.req.param('wid'),
    )
    return c.json({
      agents: agents.map((a) => ({ id: a.id, handle: a.display_name, kind: 'agent' as const })),
      humans: humans.map((h) => ({ id: h.id, handle: h.name, kind: 'human' as const })),
    })
  })

  // ---------- membership ----------

  app.post('/api/channels/:cid/members', async (c) => {
    const cid = c.req.param('cid')
    const parsed = zAddMember.safeParse(await c.req.json().catch(() => null))
    if (!parsed.success) return bad(c, parsed.error.message)
    const { memberId, memberKind } = parsed.data
    const ch = db.get<{ workspace_id: string }>('SELECT workspace_id FROM channel WHERE id = ?', cid)
    if (!ch) return notFound(c, 'channel not found')
    const exists =
      memberKind === 'agent'
        ? db.get<{ id: string }>('SELECT id FROM agent WHERE id = ? AND workspace_id = ?', memberId, ch.workspace_id)
        : db.get<{ id: string }>('SELECT id FROM human WHERE id = ? AND workspace_id = ?', memberId, ch.workspace_id)
    if (!exists) return bad(c, `${memberKind} ${memberId} not found in workspace`)
    db.run(
      'INSERT OR IGNORE INTO channel_member(channel_id, member_id, member_kind) VALUES (?,?,?)',
      cid, memberId, memberKind,
    )
    return c.json({ ok: true }, 201)
  })

  // ---------- threads ----------

  app.post('/api/channels/:cid/threads', async (c) => {
    const cid = c.req.param('cid')
    const channel = db.get<{ id: string }>('SELECT id FROM channel WHERE id = ?', cid)
    if (!channel) return notFound(c, 'channel not found')
    const parsed = zCreateThread.safeParse(await c.req.json().catch(() => null))
    if (!parsed.success) return bad(c, parsed.error.message)
    const { title, body, authorId, authorKind } = parsed.data
    const threadId = newId('thr')
    db.run('INSERT INTO thread(id, channel_id, title, created_at) VALUES (?,?,?,?)', threadId, cid, title, now())
    const message = postMessage(db, cid, {
      body,
      authorId,
      authorKind,
      threadId,
      broadcastPolicyOverride: null,
      contextScope: 'thread',
    })
    return c.json({ threadId, message }, 201)
  })

  // ---------- messages (critical path) ----------

  app.get('/api/channels/:cid/messages', (c) => {
    const cid = c.req.param('cid')
    const threadId = c.req.query('threadId')
    const rows: RawMessage[] = threadId
      ? db.all<RawMessage>(
          'SELECT id, channel_id, thread_id, author_id, author_kind, body, mentions, notices, created_at FROM message WHERE thread_id = ? ORDER BY created_at',
          threadId,
        )
      : db.all<RawMessage>(
          'SELECT id, channel_id, thread_id, author_id, author_kind, body, mentions, notices, created_at FROM message WHERE channel_id = ? AND thread_id IS NULL ORDER BY created_at',
          cid,
        )
    const ids = rows.map((r) => r.id)
    const deliveriesByMsg = loadDeliveriesForMessages(db, ids)
    return c.json(rows.map((r) => enrichMessage(db, r, deliveriesByMsg.get(r.id) ?? [])))
  })

  app.post('/api/channels/:cid/messages', async (c) => {
    const cid = c.req.param('cid')
    const channel = db.get<{ id: string }>('SELECT id FROM channel WHERE id = ?', cid)
    if (!channel) return notFound(c, 'channel not found')
    const parsed = zPostMessage.safeParse(await c.req.json().catch(() => null))
    if (!parsed.success) return bad(c, parsed.error.message)
    const message = postMessage(db, cid, {
      body: parsed.data.body,
      authorId: parsed.data.authorId,
      authorKind: parsed.data.authorKind,
      threadId: parsed.data.threadId ?? null,
      broadcastPolicyOverride: parsed.data.broadcastPolicyOverride ?? null,
      contextScope: parsed.data.contextScope ?? null,
    })
    return c.json({ message }, 201)
  })

  app.get('/api/messages/:mid/deliveries', (c) => {
    const rows = db.all<{
      id: string
      message_id: string
      recipient_id: string
      recipient_kind: MemberKind
      delivery_state: 'delivered' | 'excluded' | 'deferred'
      wake: number
      reason_code: string
      reason_detail: string
      matched_rule_id: string
    }>(
      'SELECT id, message_id, recipient_id, recipient_kind, delivery_state, wake, reason_code, reason_detail, matched_rule_id FROM message_delivery WHERE message_id = ? ORDER BY delivery_state, recipient_kind',
      c.req.param('mid'),
    )
    return c.json(rows.map((r) => ({ ...r, recipientHandle: handleOf(db, r.recipient_id, r.recipient_kind), wake: r.wake === 1 })))
  })

  // ---------- machines + instances (runtime bridge setup, D-024) ----------

  app.get('/api/workspaces/:wid/machines', (c) => {
    const rows = db.all<{
      id: string
      workspace_id: string
      name: string
      owner: string | null
      token_suffix: string | null
    }>(
      'SELECT id, workspace_id, name, owner, token_suffix FROM machine WHERE workspace_id = ? ORDER BY created_at',
      c.req.param('wid'),
    )
    const views: MachineView[] = rows.map((r) => ({
      id: r.id,
      workspaceId: r.workspace_id,
      name: r.name,
      owner: r.owner,
      tokenSuffix: r.token_suffix,
    }))
    return c.json(views)
  })

  app.post('/api/workspaces/:wid/machines', async (c) => {
    const wid = c.req.param('wid')
    const ws = db.get<{ id: string }>('SELECT id FROM workspace WHERE id = ?', wid)
    if (!ws) return notFound(c, 'workspace not found')
    const parsed = zRegisterMachine.safeParse(await c.req.json().catch(() => null))
    if (!parsed.success) return bad(c, parsed.error.message)
    try {
      const { machine, token } = registerMachine(db, wid, parsed.data.name, parsed.data.owner ?? null)
      return c.json({ ...machine, token }, 201)
    } catch (e) {
      return errResponse(c, e)
    }
  })

  app.post('/api/workspaces/:wid/agents/:aid/instances', async (c) => {
    const wid = c.req.param('wid')
    const ws = db.get<{ id: string }>('SELECT id FROM workspace WHERE id = ?', wid)
    if (!ws) return notFound(c, 'workspace not found')
    const parsed = zUpsertInstance.safeParse(await c.req.json().catch(() => null))
    if (!parsed.success) return bad(c, parsed.error.message)
    try {
      return c.json(upsertInstance(db, wid, c.req.param('aid'), parsed.data), 200)
    } catch (e) {
      return errResponse(c, e)
    }
  })

  // ---------- machine runtime bridge (bearer-authed) ----------

  app.post('/api/machines/:mid/heartbeat', (c) => {
    const mid = c.req.param('mid')
    try {
      const auth = authMachineByToken(db, bearerOf(c))
      if (auth.id !== mid) return c.json({ error: 'token does not match machine' }, 403)
      return c.json(heartbeat(db, mid))
    } catch (e) {
      return errResponse(c, e)
    }
  })

  app.get('/api/machines/:mid/dispatches', (c) => {
    const mid = c.req.param('mid')
    const limit = Math.min(Math.max(Number(c.req.query('limit') ?? 16), 1), 64)
    try {
      const auth = authMachineByToken(db, bearerOf(c))
      if (auth.id !== mid) return c.json({ error: 'token does not match machine' }, 403)
      return c.json(pollDispatches(db, mid, limit))
    } catch (e) {
      return errResponse(c, e)
    }
  })

  app.post('/api/dispatches/:id/claim', (c) => {
    try {
      const auth = authMachineByToken(db, bearerOf(c))
      return c.json(claimDispatch(db, c.req.param('id'), auth), 200)
    } catch (e) {
      return errResponse(c, e)
    }
  })

  app.post('/api/dispatches/:id/complete', async (c) => {
    const parsed = zCompleteDispatch.safeParse(await c.req.json().catch(() => null))
    if (!parsed.success) return bad(c, parsed.error.message)
    try {
      const auth = authMachineByToken(db, bearerOf(c))
      return c.json(completeDispatch(db, c.req.param('id'), auth, parsed.data), 200)
    } catch (e) {
      return errResponse(c, e)
    }
  })

  app.post('/api/dispatches/:id/abandon', (c) => {
    try {
      const auth = authMachineByToken(db, bearerOf(c))
      return c.json(abandonDispatch(db, c.req.param('id'), auth), 200)
    } catch (e) {
      return errResponse(c, e)
    }
  })

  // ---------- seed ----------

  app.post('/api/seed/pm-scenario', (c) => c.json(seedPmScenario(db), 201))

  return app
}
