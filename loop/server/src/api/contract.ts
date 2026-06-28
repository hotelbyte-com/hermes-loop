// API contracts: zod input schemas + output view types.
//
// Output view types are the shapes the web UI consumes. They are constructed by
// api/routes.ts from raw DB rows; the web mirrors the minimal subset it needs.

import { z } from 'zod'

import type { BroadcastPolicy, ContextScope, Mention } from '../delivery/types.ts'

// ---------- inputs ----------

export const memberKind = z.enum(['human', 'agent'])

export const broadcastPolicySchema = z.object({
  defaultAudience: z.enum(['mentioned', 'members']),
  allowAtAll: z.boolean(),
  allowAtOnline: z.boolean(),
})

export const zCreateWorkspace = z.object({
  slug: z.string().min(1).max(64).regex(/^[a-z0-9-]+$/),
  name: z.string().min(1).max(120),
})

export const zCreateChannel = z.object({
  name: z.string().min(1).max(80),
  kind: z.enum(['channel', 'dm']).optional(),
  broadcastPolicy: broadcastPolicySchema.optional(),
  contextScope: z.enum(['channel', 'thread', 'private']).optional(),
})

export const zPostMessage = z.object({
  body: z.string().min(1).max(8000),
  authorId: z.string().min(1),
  authorKind: memberKind,
  threadId: z.string().nullish(),
  broadcastPolicyOverride: broadcastPolicySchema.nullish(),
  contextScope: z.enum(['channel', 'thread', 'private']).nullish(),
})

export const zCreateThread = z.object({
  title: z.string().min(1).max(200),
  body: z.string().min(1).max(8000),
  authorId: z.string().min(1),
  authorKind: memberKind,
})

export const zAddMember = z.object({
  memberId: z.string().min(1),
  memberKind: memberKind,
})

export type PostMessageInput = z.infer<typeof zPostMessage>

// ---------- output views ----------

export type MemberKind = 'human' | 'agent'

export type DeliveryView = {
  id: string
  recipientId: string
  recipientKind: MemberKind
  recipientHandle: string
  state: 'delivered' | 'excluded' | 'deferred'
  wake: boolean
  reasonCode: string
  reasonDetail: string
  matchedRuleId: string
}

export type NoticeView = { code: string; detail: string }

export type MessageView = {
  id: string
  channelId: string
  threadId: string | null
  authorId: string
  authorKind: MemberKind
  authorHandle: string
  body: string
  mentions: Mention[]
  createdAt: number
  deliveries: DeliveryView[]
  notices: NoticeView[]
}

export type ChannelView = {
  id: string
  workspaceId: string
  name: string
  kind: string
  broadcastPolicy: BroadcastPolicy
  contextScope: ContextScope
  memberCount: number
  createdAt: number
}

export type AgentView = {
  id: string
  displayName: string
  soulName: string
  role: string | null
  description: string | null
  online: boolean
  runtime: string | null
}

export type HumanView = {
  id: string
  name: string
}

export type WorkspaceView = {
  id: string
  slug: string
  name: string
  createdAt: number
}
