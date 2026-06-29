// The message delivery critical path, factored out so both the HTTP layer
// (api/routes.ts) and the seed (seed/pm-scenario.ts) share ONE implementation.

import type { Db } from '../db/client.ts'
import { newId, now } from '../db/id.ts'
import { decideDelivery } from '../delivery/decider.ts'
import { parseMentions, type HandleResolver } from '../delivery/parser.ts'
import type {
  BroadcastPolicy,
  Candidate,
  ContextScope,
  DeliveryVerdict,
  MemberKind,
  Mention,
} from '../delivery/types.ts'
import type { MessageView } from './contract.ts'
import { enrichMessage, handleOf, type DispatchPayload, type RawDelivery, type RawMember, type RawMessage } from './views.ts'

export type PostMessageArgs = {
  body: string
  authorId: string
  authorKind: MemberKind
  threadId: string | null
  broadcastPolicyOverride: BroadcastPolicy | null
  contextScope: ContextScope | null
  // D-026: the task this message anchors (set on synthesized assignment messages so the
  // spawned dispatch carries dispatch.task_id, anchoring it to the task in the same tx).
  taskId?: string | null
  // D-026: the agent assignees woken by this message (passed into decideDelivery so the
  // decider's step 2.5 emits TASK_ASSIGNEE — wake driven structurally, never by body parsing).
  taskAssigneeIds?: Set<string>
}

export function buildCandidates(db: Db, channelId: string): Candidate[] {
  const members = db.all<RawMember>(
    'SELECT member_id, member_kind FROM channel_member WHERE channel_id = ? ORDER BY member_kind, member_id',
    channelId,
  )
  return members.map((m) => {
    if (m.member_kind === 'agent') {
      const a = db.get<{ display_name: string }>(
        'SELECT display_name FROM agent WHERE id = ?',
        m.member_id,
      )
      const inst = db.get<{ n: number }>(
        'SELECT COUNT(*) AS n FROM instance WHERE agent_id = ? AND online = 1',
        m.member_id,
      )
      return {
        memberId: m.member_id,
        memberKind: 'agent',
        handle: a?.display_name ?? 'agent',
        online: (inst?.n ?? 0) > 0,
      }
    }
    return {
      memberId: m.member_id,
      memberKind: 'human',
      handle: handleOf(db, m.member_id, 'human'),
      online: true,
    }
  })
}

export function buildResolver(candidates: Candidate[]): HandleResolver {
  const index = new Map<string, { memberId: string; memberKind: MemberKind }>()
  for (const c of candidates) {
    index.set(c.handle.toLowerCase(), { memberId: c.memberId, memberKind: c.memberKind })
  }
  return (handle) => index.get(handle.toLowerCase())
}

function threadParticipants(db: Db, threadId: string): Set<string> {
  const rows = db.all<{ author_id: string; mentions: string }>(
    'SELECT author_id, mentions FROM message WHERE thread_id = ?',
    threadId,
  )
  const set = new Set<string>()
  for (const r of rows) {
    set.add(r.author_id)
    const mentions = JSON.parse(r.mentions || '[]') as Mention[]
    for (const m of mentions) {
      if (m.kind === 'member') set.add(m.memberId)
    }
  }
  return set
}

// Load channel policy/scope, run the decider, persist message + delivery snapshots.
export function postMessage(db: Db, channelId: string, args: PostMessageArgs): MessageView {
  const channel = db.get<{ workspace_id: string; broadcast_policy: string; context_scope: string }>(
    'SELECT workspace_id, broadcast_policy, context_scope FROM channel WHERE id = ?',
    channelId,
  )
  if (!channel) throw new Error(`channel not found: ${channelId}`)

  const policy: BroadcastPolicy =
    args.broadcastPolicyOverride ?? (JSON.parse(channel.broadcast_policy) as BroadcastPolicy)
  const scope: ContextScope = args.contextScope ?? (channel.context_scope as ContextScope)
  const candidates = buildCandidates(db, channelId)
  const mentions = parseMentions(args.body, buildResolver(candidates))
  // Thread participants = prior authors/mentions in the thread PLUS this message's author
  // and its own member-mentions, so the FIRST message in a fresh thread still has a non-empty,
  // correct participant set (review finding: otherwise the thread seed message wakes nobody).
  let threadParticipantIds: Set<string> | undefined
  if (scope === 'thread' && args.threadId) {
    threadParticipantIds = threadParticipants(db, args.threadId)
    threadParticipantIds.add(args.authorId)
    for (const m of mentions) {
      if (m.kind === 'member') threadParticipantIds.add(m.memberId)
    }
  }

  const result = decideDelivery({
    authorId: args.authorId,
    authorKind: args.authorKind,
    mentions,
    policy,
    scope,
    threadParticipantIds,
    candidates,
    taskAssigneeIds: args.taskAssigneeIds,
  })

  const msgId = newId('msg')
  const ts = now()
  const mentionsJson = JSON.stringify(mentions)
  const noticesJson = JSON.stringify(result.notices)
  const authorHandle = handleOf(db, args.authorId, args.authorKind)

  db.transaction(() => {
    db.run(
      'INSERT INTO message(id, channel_id, thread_id, author_id, author_kind, body, mentions, broadcast_policy_override, context_scope, notices, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
      msgId,
      channelId,
      args.threadId,
      args.authorId,
      args.authorKind,
      args.body,
      mentionsJson,
      args.broadcastPolicyOverride ? JSON.stringify(args.broadcastPolicyOverride) : null,
      args.contextScope,
      noticesJson,
      ts,
    )
    // Persist each delivery verdict, keeping the generated id so we can link a dispatch
    // to its triggering delivery in the same transaction.
    const deliveries: { verdict: DeliveryVerdict; deliveryId: string }[] = []
    for (const v of result.verdicts) {
      const deliveryId = newId('dlv')
      deliveries.push({ verdict: v, deliveryId })
      db.run(
        'INSERT INTO message_delivery(id, message_id, recipient_id, recipient_kind, delivery_state, wake, reason_code, reason_detail, matched_rule_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
        deliveryId,
        msgId,
        v.recipientId,
        v.recipientKind,
        v.state,
        v.wake ? 1 : 0,
        v.reasonCode,
        v.reasonDetail,
        v.matchedRuleId,
        ts,
      )
    }
    // D-024 runtime bridge: a WAKE delivery to an agent spawns a dispatch targeting it.
    // Driven by the structured verdict.wake flag — never substring matching. Quiet default
    // (CHANNEL_BROADCAST) and DEFERRED_OFFLINE produce no dispatch, preserving the moat:
    // an offline @online agent is not woken, and a plain channel message does not fan out.
    for (const { verdict, deliveryId } of deliveries) {
      if (verdict.recipientKind === 'agent' && verdict.wake && verdict.state === 'delivered') {
        spawnDispatch(db, {
          messageId: msgId,
          deliveryId,
          workspaceId: channel.workspace_id,
          channelId,
          threadId: args.threadId,
          agentId: verdict.recipientId,
          reasonCode: verdict.reasonCode,
          body: args.body,
          authorId: args.authorId,
          authorKind: args.authorKind,
          authorHandle,
          scope,
          taskId: args.taskId ?? null,
          ts,
        })
      }
    }
  })

  // Reload message + deliveries from the DB so the returned view carries the REAL
  // delivery ids — needed to resolve the linked dispatch projection (a freshly posted
  // wake-agent delivery must show its pending dispatch immediately, not only after a
  // GET reload). One extra read of the row we just wrote; cheap and correct.
  const raw = db.get<RawMessage>(
    'SELECT id, channel_id, thread_id, author_id, author_kind, body, mentions, notices, created_at FROM message WHERE id = ?',
    msgId,
  )!
  const rawDeliveries = db.all<RawDelivery>(
    'SELECT id, message_id, recipient_id, recipient_kind, delivery_state, wake, reason_code, reason_detail, matched_rule_id FROM message_delivery WHERE message_id = ?',
    msgId,
  )
  return enrichMessage(db, raw, rawDeliveries)
}

// Record one runtime dispatch for a wake-agent delivery. Runs inside the caller's
// transaction so message + deliveries + dispatch commit atomically. `runtime` is the
// agent's most-recent instance runtime (informational — the dispatch is runtime-agnostic
// and any eligible machine may claim it). state defaults to 'pending'.
type SpawnDispatchArgs = {
  messageId: string
  deliveryId: string
  workspaceId: string
  channelId: string
  threadId: string | null
  agentId: string
  reasonCode: string
  body: string
  authorId: string
  authorKind: MemberKind
  authorHandle: string
  scope: ContextScope
  taskId: string | null
  ts: number
}

function spawnDispatch(db: Db, a: SpawnDispatchArgs): void {
  const inst = db.get<{ runtime: string }>(
    'SELECT runtime FROM instance WHERE agent_id = ? ORDER BY online DESC, last_seen_at DESC LIMIT 1',
    a.agentId,
  )
  const payload: DispatchPayload = {
    body: a.body,
    authorId: a.authorId,
    authorKind: a.authorKind,
    authorHandle: a.authorHandle,
    reasonCode: a.reasonCode,
    // Captured at wake time so the agent reply re-enters the SAME scope the waking
    // message had (review finding: avoid inheriting the channel row's current scope).
    contextScope: a.scope,
    createdAt: a.ts,
  }
  db.run(
    'INSERT INTO dispatch(id, message_id, delivery_id, task_id, workspace_id, channel_id, thread_id, agent_id, runtime, payload, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
    newId('dsp'),
    a.messageId,
    a.deliveryId,
    a.taskId,
    a.workspaceId,
    a.channelId,
    a.threadId,
    a.agentId,
    inst?.runtime ?? null,
    JSON.stringify(payload),
    a.ts,
  )
}
