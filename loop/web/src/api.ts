import type {
  AgentView,
  ChannelView,
  HumanView,
  MessageView,
  SeedResult,
} from './types.ts'

const BASE = '/api'

async function asJson<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json() as Promise<T>
}

const post = (url: string, body: unknown) =>
  fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })

export const api = {
  health: () => fetch(`${BASE}/health`).then((r) => r.json()),
  seed: () => post(`${BASE}/seed/pm-scenario`, {}).then(asJson<SeedResult>),
  workspaces: () => fetch(`${BASE}/workspaces`).then(asJson<{ id: string }[]>),
  channels: (wid: string) =>
    fetch(`${BASE}/workspaces/${wid}/channels`).then(asJson<ChannelView[]>),
  agents: (wid: string) =>
    fetch(`${BASE}/workspaces/${wid}/agents`).then(asJson<AgentView[]>),
  humans: (wid: string) =>
    fetch(`${BASE}/workspaces/${wid}/humans`).then(asJson<HumanView[]>),
  messages: (cid: string) =>
    fetch(`${BASE}/channels/${cid}/messages`).then(asJson<MessageView[]>),
  postMessage: (
    cid: string,
    body: {
      body: string
      authorId: string
      authorKind: 'human' | 'agent'
      broadcastPolicyOverride?: unknown
    },
  ) =>
    post(`${BASE}/channels/${cid}/messages`, body).then(
      asJson<{ message: MessageView }>,
    ),
}
