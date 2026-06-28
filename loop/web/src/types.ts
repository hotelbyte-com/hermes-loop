export type MemberKind = 'human' | 'agent'

export interface DeliveryView {
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
  dispatch?: { state: DispatchState; runtime: string | null } | null
}

export type DispatchState = 'pending' | 'claimed' | 'done' | 'failed' | 'dead'

export interface NoticeView {
  code: string
  detail: string
}

export interface Mention {
  kind: 'all' | 'online' | 'member'
  memberId?: string
  memberKind?: MemberKind
  handle?: string
}

export interface MessageView {
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

export interface BroadcastPolicy {
  defaultAudience: 'mentioned' | 'members'
  allowAtAll: boolean
  allowAtOnline: boolean
}

export interface ChannelView {
  id: string
  workspaceId: string
  name: string
  kind: string
  broadcastPolicy: BroadcastPolicy
  contextScope: string
  memberCount: number
  createdAt: number
}

export interface AgentView {
  id: string
  displayName: string
  soulName: string
  role: string | null
  description: string | null
  online: boolean
  runtime: string | null
}

export interface HumanView {
  id: string
  name: string
}

export interface SeedResult {
  workspaceId: string
  channelId: string
  aliceId: string
  botAgentIds: Record<string, string>
  demoMessages: number
}
