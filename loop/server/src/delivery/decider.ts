// THE DELIVERY DECIDER — the product moat (D-022: controllable delivery boundary).
//
// Pure function: DeliveryInput -> DeliveryResult. No DB, no I/O, fully deterministic
// and data-driven. Decisions are made ONLY over structured inputs:
//   parsed mention tokens + channel membership + online state + typed policy + scope.
//
// It NEVER inspects message body substrings and NEVER uses a hardcoded rule table to
// decide agent behavior (CLAUDE.md agent/LLM hard-ban). Every outcome carries a typed
// ReasonCode + human-readable detail so the diagnostics panel can explain it.
//
// Precedence per candidate (after self-exclusion + scope gate):
//   1. direct @mention          -> DIRECT_MENTION (wake)
//   2. @all (policy allows)     -> ALL_BROADCAST (wake)
//   3. @online (policy allows)  -> ONLINE_BROADCAST (wake) / DEFERRED_OFFLINE (offline)
//   4. default audience         -> CHANNEL_BROADCAST (silent) | EXCLUDED_NOT_MENTIONED
// A blocked @all/@online is recorded as a message-level notice (not a per-recipient
// exclusion) so recipients still resolve via the default audience.

import type {
  Candidate,
  DeliveryInput,
  DeliveryNotice,
  DeliveryResult,
  DeliveryVerdict,
  Mention,
  ReasonCode,
} from './types.ts'

type MemberMention = Extract<Mention, { kind: 'member' }>

function isMemberMention(m: Mention): m is MemberMention {
  return m.kind === 'member'
}

export function decideDelivery(input: DeliveryInput): DeliveryResult {
  const {
    authorId,
    authorKind,
    mentions,
    policy,
    scope,
    threadParticipantIds,
    candidates,
    taskAssigneeIds,
  } = input

  const mentionedIds = new Set(mentions.filter(isMemberMention).map((m) => m.memberId))
  const hasAll = mentions.some((m) => m.kind === 'all')
  const hasOnline = mentions.some((m) => m.kind === 'online')

  const allHit = hasAll && policy.allowAtAll
  const onlineHit = hasOnline && policy.allowAtOnline

  const notices: DeliveryNotice[] = []
  if (hasAll && !policy.allowAtAll) {
    notices.push({ code: 'BROADCAST_BLOCKED', detail: '@all is disabled by this channel policy' })
  }
  if (hasOnline && !policy.allowAtOnline) {
    notices.push({ code: 'BROADCAST_BLOCKED', detail: '@online is disabled by this channel policy' })
  }

  const verdicts: DeliveryVerdict[] = []

  for (const c of candidates) {
    // 1) Author never delivers to self.
    if (c.memberId === authorId && c.memberKind === authorKind) {
      verdicts.push(deny(c, 'EXCLUDED_SELF', 'author is not a recipient of their own message', 'self'))
      continue
    }
    // 2) Context scope gate.
    if (scope === 'private' && !mentionedIds.has(c.memberId)) {
      verdicts.push(deny(c, 'EXCLUDED_CONTEXT_SCOPE', 'private scope: only mentioned recipients', 'scope.private'))
      continue
    }
    if (
      scope === 'thread' &&
      threadParticipantIds &&
      !threadParticipantIds.has(c.memberId) &&
      !mentionedIds.has(c.memberId)
    ) {
      verdicts.push(deny(c, 'EXCLUDED_CONTEXT_SCOPE', 'thread scope: not a thread participant', 'scope.thread'))
      continue
    }
    // 2.5) Task assignment wake — the 4th explicit wake (D-026). MUST precede DIRECT_MENTION so
    // the synthesized assignment message (whose mentions DELIBERATELY omit the assignee member
    // token) resolves here, keeping TASK_ASSIGNEE reachable and distinguishing woken-by-assignment
    // from woken-by-@. Driven entirely by the structured taskAssigneeIds field, never body parsing.
    if (c.memberKind === 'agent' && taskAssigneeIds?.has(c.memberId)) {
      verdicts.push(allow(c, true, 'TASK_ASSIGNEE', 'task assigned', 'task.assignee'))
      continue
    }
    // 3) Direct @mention always wins.
    if (mentionedIds.has(c.memberId)) {
      verdicts.push(allow(c, true, 'DIRECT_MENTION', 'explicitly @mentioned', 'mention'))
      continue
    }
    // 4) @all broadcast.
    if (allHit) {
      verdicts.push(allow(c, true, 'ALL_BROADCAST', '@all broadcast, policy allows', 'at-all'))
      continue
    }
    // 5) @online broadcast (online wake / offline defer).
    if (onlineHit) {
      if (c.online) {
        verdicts.push(allow(c, true, 'ONLINE_BROADCAST', '@online broadcast, member online', 'at-online'))
      } else {
        verdicts.push(defer(c, 'DEFERRED_OFFLINE', '@online broadcast, member offline', 'at-online.offline'))
      }
      continue
    }
    // 6) Default audience — the controllable-delivery boundary.
    if (policy.defaultAudience === 'members') {
      verdicts.push(allow(c, false, 'CHANNEL_BROADCAST', 'channel default includes all members (silent)', 'default.members'))
    } else {
      verdicts.push(deny(c, 'EXCLUDED_NOT_MENTIONED', 'quiet default: only @mentioned recipients wake', 'default.mentioned'))
    }
  }

  return { verdicts, notices }
}

function allow(c: Candidate, wake: boolean, code: ReasonCode, detail: string, rule: string): DeliveryVerdict {
  return {
    recipientId: c.memberId,
    recipientKind: c.memberKind,
    state: 'delivered',
    wake,
    reasonCode: code,
    reasonDetail: detail,
    matchedRuleId: rule,
  }
}

function deny(c: Candidate, code: ReasonCode, detail: string, rule: string): DeliveryVerdict {
  return {
    recipientId: c.memberId,
    recipientKind: c.memberKind,
    state: 'excluded',
    wake: false,
    reasonCode: code,
    reasonDetail: detail,
    matchedRuleId: rule,
  }
}

function defer(c: Candidate, code: ReasonCode, detail: string, rule: string): DeliveryVerdict {
  return {
    recipientId: c.memberId,
    recipientKind: c.memberKind,
    state: 'deferred',
    wake: false,
    reasonCode: code,
    reasonDetail: detail,
    matchedRuleId: rule,
  }
}
