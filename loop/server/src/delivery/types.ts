// Typed contracts for the delivery decider.
//
// Everything the decider reasons over is a STRUCTURED value defined here. There is
// no "body string" type and no keyword/rule table — by construction the decider
// cannot route on raw message text (CLAUDE.md agent/LLM hard-ban is honored).

export type MemberKind = 'human' | 'agent'

// A closed enum of delivery outcomes. Every MessageDelivery row carries one.
export type ReasonCode =
  | 'DIRECT_MENTION'              // explicitly @mentioned (parsed token)
  | 'CHANNEL_BROADCAST'           // channel default audience includes all members (silent)
  | 'ALL_BROADCAST'               // @all hit and policy allowed
  | 'ONLINE_BROADCAST'            // @online hit and member was online
  | 'DEFERRED_OFFLINE'            // @online hit but member offline — deferred, not woken
  | 'EXCLUDED_NOT_MENTIONED'      // quiet default: only @mentioned recipients wake
  | 'EXCLUDED_BROADCAST_BLOCKED'  // (legacy/audit) broadcast was blocked at message level
  | 'EXCLUDED_CONTEXT_SCOPE'      // message scope (thread/private) excluded recipient
  | 'EXCLUDED_SELF'               // author never delivers to self

export type DeliveryState = 'delivered' | 'excluded' | 'deferred'

// Parsed @mention token. Produced by delivery/parser.ts (protocol tokenization).
export type Mention =
  | { kind: 'all' }
  | { kind: 'online' }
  | { kind: 'member'; memberId: string; memberKind: MemberKind; handle: string }

// Per-channel broadcast policy. `defaultAudience: 'mentioned'` is the QUIET DEFAULT
// — the controllable-delivery boundary (the moat). Ordinary messages do NOT fan out.
export type BroadcastPolicy = {
  defaultAudience: 'mentioned' | 'members'
  allowAtAll: boolean
  allowAtOnline: boolean
}

export type ContextScope = 'channel' | 'thread' | 'private'

// A candidate recipient, resolved from channel membership + instance online state.
export type Candidate = {
  memberId: string
  memberKind: MemberKind
  handle: string
  online: boolean
}

export type DeliveryInput = {
  authorId: string
  authorKind: MemberKind
  mentions: Mention[]
  policy: BroadcastPolicy
  scope: ContextScope
  threadParticipantIds?: Set<string>
  candidates: Candidate[]
}

export type DeliveryVerdict = {
  recipientId: string
  recipientKind: MemberKind
  state: DeliveryState
  wake: boolean
  reasonCode: ReasonCode
  reasonDetail: string
  matchedRuleId: string
}

// Message-level diagnostics (e.g. a broadcast keyword was used but blocked).
export type DeliveryNotice = { code: string; detail: string }

export type DeliveryResult = {
  verdicts: DeliveryVerdict[]
  notices: DeliveryNotice[]
}

export const DEFAULT_POLICY: BroadcastPolicy = {
  defaultAudience: 'mentioned',
  allowAtAll: true,
  allowAtOnline: true,
}
