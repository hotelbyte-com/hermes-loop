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
import { enrichMessage, handleOf, type RawDelivery, type RawMember, type RawMessage } from './views.ts'

export type PostMessageArgs = {
  body: string
  authorId: string
  authorKind: MemberKind
  threadId: string | null
  broadcastPolicyOverride: BroadcastPolicy | null
  contextScope: ContextScope | null
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
  const channel = db.get<{ broadcast_policy: string; context_scope: string }>(
    'SELECT broadcast_policy, context_scope FROM channel WHERE id = ?',
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
  })

  const msgId = newId('msg')
  const ts = now()
  const mentionsJson = JSON.stringify(mentions)
  const noticesJson = JSON.stringify(result.notices)

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
    for (const v of result.verdicts) {
      db.run(
        'INSERT INTO message_delivery(id, message_id, recipient_id, recipient_kind, delivery_state, wake, reason_code, reason_detail, matched_rule_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
        newId('dlv'),
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
  })

  const raw: RawMessage = {
    id: msgId,
    channel_id: channelId,
    thread_id: args.threadId,
    author_id: args.authorId,
    author_kind: args.authorKind,
    body: args.body,
    mentions: mentionsJson,
    notices: noticesJson,
    created_at: ts,
  }
  const rawDeliveries: RawDelivery[] = result.verdicts.map((v: DeliveryVerdict) => ({
    id: '',
    message_id: msgId,
    recipient_id: v.recipientId,
    recipient_kind: v.recipientKind,
    delivery_state: v.state,
    wake: v.wake ? 1 : 0,
    reason_code: v.reasonCode,
    reason_detail: v.reasonDetail,
    matched_rule_id: v.matchedRuleId,
  }))
  return enrichMessage(db, raw, rawDeliveries)
}
