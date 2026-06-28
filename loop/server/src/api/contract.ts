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

export const zRegisterMachine = z.object({
  name: z.string().min(1).max(80),
  owner: z.string().max(80).optional(),
})

export const zUpsertInstance = z.object({
  machineId: z.string().min(1),
  runtime: z.string().min(1).max(64),
  online: z.boolean(),
})

export const zCompleteDispatch = z.object({
  ok: z.boolean(),
  replyBody: z.string().min(1).max(8000).optional(),
  error: z.string().max(2000).optional(),
})

// ---------- tasks (parent/child work items, D-015 / roadmap M3 §D.2.3 W7) ----------

export const taskStatus = z.enum(['open', 'in_progress', 'done', 'cancelled'])
export type TaskStatus = z.infer<typeof taskStatus>

// assigneeId and assigneeKind are coupled: either both present or both absent.
export const zCreateTask = z
  .object({
    title: z.string().min(1).max(400),
    parentTaskId: z.string().min(1).nullish(),
    threadId: z.string().min(1).nullish(),
    assigneeId: z.string().min(1).nullish(),
    assigneeKind: memberKind.nullish(),
  })
  .refine((d) => (d.assigneeId ? !!d.assigneeKind : !d.assigneeKind), {
    message: 'assigneeId requires assigneeKind',
  })

export const zUpdateTaskStatus = z.object({
  status: taskStatus,
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
  // Present only when this wake-delivery spawned a runtime dispatch (agent recipients).
  // Null otherwise (excluded/deferred/human/quiet-default produce no dispatch).
  dispatch: { state: DispatchState; runtime: string | null } | null
}

export type DispatchState = 'pending' | 'claimed' | 'done' | 'failed' | 'dead'

export type DispatchResultView = { ok: boolean; replyBody?: string; error?: string }

export type DispatchView = {
  id: string
  messageId: string
  deliveryId: string
  channelId: string
  threadId: string | null
  agentId: string
  agentHandle: string
  runtime: string | null
  state: DispatchState
  payload: {
    body: string
    authorHandle: string
    reasonCode: string
    createdAt: number
  }
  result: DispatchResultView | null
  claimedByMachine: string | null
  claimedAt: number | null
  completedAt: number | null
  createdAt: number
}

export type MachineView = {
  id: string
  workspaceId: string
  name: string
  owner: string | null
  tokenSuffix: string | null
}

export type InstanceView = {
  id: string
  agentId: string
  machineId: string
  runtime: string
  online: boolean
  lastSeenAt: number | null
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

export type TaskView = {
  id: string
  workspaceId: string
  parentTaskId: string | null
  threadId: string | null
  assigneeId: string | null
  assigneeKind: MemberKind | null
  assigneeHandle: string | null
  title: string
  status: TaskStatus
  createdAt: number
}
