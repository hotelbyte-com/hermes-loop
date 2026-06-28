// Row -> view mappers. Pure presentation; no delivery logic lives here.

import type { Db } from '../db/client.ts'
import type { BroadcastPolicy, ContextScope, MemberKind, Mention } from '../delivery/types.ts'
import type {
  ChannelView,
  DeliveryView,
  DispatchResultView,
  DispatchState,
  DispatchView,
  InstanceView,
  MachineView,
  MessageView,
  NoticeView,
  WorkspaceView,
} from './contract.ts'

export type RawMember = { member_id: string; member_kind: MemberKind }

export type RawMessage = {
  id: string
  channel_id: string
  thread_id: string | null
  author_id: string
  author_kind: MemberKind
  body: string
  mentions: string
  notices: string
  created_at: number
}

export type RawDelivery = {
  id: string
  message_id: string
  recipient_id: string
  recipient_kind: MemberKind
  delivery_state: 'delivered' | 'excluded' | 'deferred'
  wake: number
  reason_code: string
  reason_detail: string
  matched_rule_id: string
}

export function handleOf(db: Db, memberId: string, memberKind: MemberKind): string {
  if (memberKind === 'agent') {
    const row = db.get<{ display_name: string }>(
      'SELECT display_name FROM agent WHERE id = ?',
      memberId,
    )
    return row?.display_name ?? 'agent'
  }
  const row = db.get<{ name: string }>('SELECT name FROM human WHERE id = ?', memberId)
  return row?.name ?? 'human'
}

export function deliveryToView(
  db: Db,
  d: RawDelivery,
  dispatch: { state: DispatchState; runtime: string | null } | null = null,
): DeliveryView {
  return {
    id: d.id,
    recipientId: d.recipient_id,
    recipientKind: d.recipient_kind,
    recipientHandle: handleOf(db, d.recipient_id, d.recipient_kind),
    state: d.delivery_state,
    wake: d.wake === 1,
    reasonCode: d.reason_code,
    reasonDetail: d.reason_detail,
    matchedRuleId: d.matched_rule_id,
    dispatch,
  }
}

export function enrichMessage(db: Db, msg: RawMessage, deliveries: RawDelivery[]): MessageView {
  const dispatchMap = loadDispatchesForDeliveries(db, deliveries.map((d) => d.id))
  return {
    id: msg.id,
    channelId: msg.channel_id,
    threadId: msg.thread_id,
    authorId: msg.author_id,
    authorKind: msg.author_kind,
    authorHandle: handleOf(db, msg.author_id, msg.author_kind),
    body: msg.body,
    mentions: JSON.parse(msg.mentions || '[]') as Mention[],
    createdAt: msg.created_at,
    deliveries: deliveries.map((d) => deliveryToView(db, d, dispatchMap.get(d.id) ?? null)),
    notices: JSON.parse(msg.notices || '[]') as NoticeView[],
  }
}

export function loadDeliveriesForMessages(
  db: Db,
  messageIds: string[],
): Map<string, RawDelivery[]> {
  const byMessage = new Map<string, RawDelivery[]>()
  if (messageIds.length === 0) return byMessage
  const placeholders = messageIds.map(() => '?').join(',')
  const rows = db.all<RawDelivery>(
    `SELECT id, message_id, recipient_id, recipient_kind, delivery_state, wake, reason_code, reason_detail, matched_rule_id
     FROM message_delivery WHERE message_id IN (${placeholders})`,
    ...messageIds,
  )
  for (const r of rows) {
    const list = byMessage.get(r.message_id) ?? []
    list.push(r)
    byMessage.set(r.message_id, list)
  }
  return byMessage
}

// 1:1 with a wake-agent delivery. Loads the slim projection embedded into DeliveryView.
export function loadDispatchesForDeliveries(
  db: Db,
  deliveryIds: string[],
): Map<string, { state: DispatchState; runtime: string | null }> {
  const out = new Map<string, { state: DispatchState; runtime: string | null }>()
  if (deliveryIds.length === 0) return out
  const placeholders = deliveryIds.map(() => '?').join(',')
  const rows = db.all<{ delivery_id: string; state: DispatchState; runtime: string | null }>(
    `SELECT delivery_id, state, runtime FROM dispatch WHERE delivery_id IN (${placeholders})`,
    ...deliveryIds,
  )
  for (const r of rows) out.set(r.delivery_id, { state: r.state, runtime: r.runtime })
  return out
}

export type RawDispatch = {
  id: string
  message_id: string
  delivery_id: string
  channel_id: string
  thread_id: string | null
  agent_id: string
  runtime: string | null
  state: DispatchState
  payload: string
  result: string | null
  claimed_by_machine: string | null
  claimed_at: number | null
  completed_at: number | null
  created_at: number
}

export type DispatchPayload = {
  body: string
  authorId: string
  authorKind: MemberKind
  authorHandle: string
  reasonCode: string
  // The scope the WAKING message had — the agent reply re-enters the SAME scope rather
  // than inheriting whatever context_scope the channel row currently has (review finding:
  // a channel-level dispatch reply could otherwise pick up a private/thread channel scope).
  contextScope: ContextScope
  createdAt: number
}

export function dispatchToView(db: Db, r: RawDispatch): DispatchView {
  const payload = JSON.parse(r.payload) as DispatchPayload
  const result = r.result ? (JSON.parse(r.result) as DispatchResultView) : null
  return {
    id: r.id,
    messageId: r.message_id,
    deliveryId: r.delivery_id,
    channelId: r.channel_id,
    threadId: r.thread_id,
    agentId: r.agent_id,
    agentHandle: handleOf(db, r.agent_id, 'agent'),
    runtime: r.runtime,
    state: r.state,
    payload: {
      body: payload.body,
      authorHandle: payload.authorHandle,
      reasonCode: payload.reasonCode,
      createdAt: payload.createdAt,
    },
    result,
    claimedByMachine: r.claimed_by_machine,
    claimedAt: r.claimed_at,
    completedAt: r.completed_at,
    createdAt: r.created_at,
  }
}

export function machineView(db: Db, id: string): MachineView {
  const r = db.get<{ id: string; workspace_id: string; name: string; owner: string | null; token_suffix: string | null }>(
    'SELECT id, workspace_id, name, owner, token_suffix FROM machine WHERE id = ?',
    id,
  )
  if (!r) throw new Error(`machine not found: ${id}`)
  return {
    id: r.id,
    workspaceId: r.workspace_id,
    name: r.name,
    owner: r.owner,
    tokenSuffix: r.token_suffix,
  }
}

export function instanceView(db: Db, id: string): InstanceView {
  const r = db.get<{
    id: string
    agent_id: string
    machine_id: string
    runtime: string
    online: number
    last_seen_at: number | null
  }>(
    'SELECT id, agent_id, machine_id, runtime, online, last_seen_at FROM instance WHERE id = ?',
    id,
  )
  if (!r) throw new Error(`instance not found: ${id}`)
  return {
    id: r.id,
    agentId: r.agent_id,
    machineId: r.machine_id,
    runtime: r.runtime,
    online: r.online === 1,
    lastSeenAt: r.last_seen_at,
  }
}

export function workspaceView(db: Db, id: string): WorkspaceView {
  const r = db.get<{ id: string; slug: string; name: string; created_at: number }>(
    'SELECT id, slug, name, created_at FROM workspace WHERE id = ?',
    id
  )
  if (!r) throw new Error(`workspace not found: ${id}`)
  return { id: r.id, slug: r.slug, name: r.name, createdAt: r.created_at }
}

export function channelView(db: Db, id: string): ChannelView {
  const r = db.get<{
    id: string
    workspace_id: string
    name: string
    kind: string
    broadcast_policy: string
    context_scope: string
    created_at: number
  }>(
    'SELECT id, workspace_id, name, kind, broadcast_policy, context_scope, created_at FROM channel WHERE id = ?',
    id,
  )!
  return {
    id: r.id,
    workspaceId: r.workspace_id,
    name: r.name,
    kind: r.kind,
    broadcastPolicy: JSON.parse(r.broadcast_policy) as BroadcastPolicy,
    contextScope: r.context_scope as ContextScope,
    memberCount: countMembers(db, r.id),
    createdAt: r.created_at,
  }
}

export function countMembers(db: Db, channelId: string): number {
  const r = db.get<{ n: number }>(
    'SELECT COUNT(*) AS n FROM channel_member WHERE channel_id = ?',
    channelId,
  )
  return r?.n ?? 0
}
