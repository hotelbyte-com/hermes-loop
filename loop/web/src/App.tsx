import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { api } from './api.ts'
import type {
  AgentView,
  ChannelView,
  DeliveryView,
  HumanView,
  MemberKind,
  MessageView,
  NoticeView,
  SeedResult,
} from './types.ts'

function fmtTime(ms: number): string {
  return new Date(ms).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

const STATE_META: Record<DeliveryView['state'], { label: string; cls: string; icon: string }> = {
  delivered: { label: '已投递', cls: 'st-delivered', icon: '✅' },
  excluded: { label: '未打扰', cls: 'st-excluded', icon: '⛔' },
  deferred: { label: '延后', cls: 'st-deferred', icon: '🕓' },
}

// Highlight @tokens in the body. This is presentation only — it does not influence
// delivery (the server's parser owns tokenization). Mirrors parser.ts's regex.
const TOKEN_RE = /(@[^\s@,;:!>()\[\]{}"']+)/g
function renderBody(body: string, mentions: MessageView['mentions']) {
  const memberHandles = new Set(
    mentions.filter((m) => m.kind === 'member').map((m) => (m.handle ?? '').toLowerCase()),
  )
  const out: ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  while ((m = TOKEN_RE.exec(body))) {
    if (m.index > last) out.push(body.slice(last, m.index))
    const tok = m[0]
    const lower = tok.toLowerCase()
    const cls =
      lower === '@all' || lower === '@online'
        ? 'mention mention-broadcast'
        : memberHandles.has(lower)
          ? 'mention mention-member'
          : null
    out.push(cls ? <span key={m.index} className={cls}>{tok}</span> : tok)
    last = m.index + tok.length
  }
  if (last < body.length) out.push(body.slice(last))
  return out
}

export function App() {
  const [seed, setSeed] = useState<SeedResult | null>(null)
  const [channels, setChannels] = useState<ChannelView[]>([])
  const [activeChannel, setActiveChannel] = useState<ChannelView | null>(null)
  const [agents, setAgents] = useState<AgentView[]>([])
  const [humans, setHumans] = useState<HumanView[]>([])
  const [messages, setMessages] = useState<MessageView[]>([])
  const [text, setText] = useState('')
  const [authorId, setAuthorId] = useState('')
  const [authorKind, setAuthorKind] = useState<MemberKind>('human')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [sending, setSending] = useState(false)
  const endRef = useRef<HTMLDivElement>(null)

  const loadChannel = useCallback(async (channel: ChannelView, wid: string) => {
    const [ag, hu, msgs] = await Promise.all([
      api.agents(wid),
      api.humans(wid),
      api.messages(channel.id),
    ])
    setAgents(ag)
    setHumans(hu)
    setMessages(msgs)
    setActiveChannel(channel)
    setExpanded(new Set())
  }, [])

  const bootstrap = useCallback(async () => {
    const s = await api.seed()
    setSeed(s)
    setAuthorId(s.aliceId)
    setAuthorKind('human')
    const chs = await api.channels(s.workspaceId)
    setChannels(chs)
    const ch = chs.find((c) => c.id === s.channelId) ?? chs[0] ?? null
    if (ch) await loadChannel(ch, s.workspaceId)
  }, [loadChannel])

  useEffect(() => {
    void (async () => {
      try {
        await bootstrap()
      } catch (e) {
        setError(String(e))
      } finally {
        setLoading(false)
      }
    })()
  }, [bootstrap])

  // Poll for live updates (multi-tab / simulated replies from other sources).
  useEffect(() => {
    if (!activeChannel) return
    const id = setInterval(async () => {
      try {
        setMessages(await api.messages(activeChannel.id))
      } catch {
        /* swallow polling errors */
      }
    }, 4000)
    return () => clearInterval(id)
  }, [activeChannel])

  // Autoscroll on new messages.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const switchChannel = useCallback(
    async (ch: ChannelView) => {
      if (!seed || ch.id === activeChannel?.id) return
      await loadChannel(ch, seed.workspaceId)
      setAuthorId(seed.aliceId)
      setAuthorKind('human')
    },
    [seed, activeChannel, loadChannel],
  )

  const createChannel = useCallback(async () => {
    if (!seed) return
    const name = window.prompt('新频道名（如 #eng-sync）')
    if (!name) return
    try {
      await fetch(`/api/workspaces/${seed.workspaceId}/channels`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      const chs = await api.channels(seed.workspaceId)
      setChannels(chs)
      const created = chs.find((c) => c.name === name)
      if (created) await switchChannel(created)
    } catch (e) {
      setError(String(e))
    }
  }, [seed, switchChannel])

  const send = useCallback(async () => {
    if (!activeChannel || !text.trim() || !authorId || sending) return
    setError(null)
    setSending(true)
    try {
      const { message } = await api.postMessage(activeChannel.id, {
        body: text.trim(),
        authorId,
        authorKind,
      })
      setMessages((prev) => [...prev, message])
      setText('')
      // Simulate a bot reply on direct mention (MVP has no real runtime yet — runtime
      // is external per D-021; this keeps the collaboration loop visibly alive).
      if (authorKind === 'human') {
        const mentioned = message.deliveries.find(
          (d) => d.reasonCode === 'DIRECT_MENTION' && d.recipientKind === 'agent' && d.state === 'delivered',
        )
        const agent = mentioned ? agents.find((a) => a.id === mentioned.recipientId) : null
        if (agent) {
          setTimeout(async () => {
            try {
              const { message: reply } = await api.postMessage(activeChannel.id, {
                body: `🤖（模拟回复）收到，我（${agent.displayName}）开始处理。${agent.online ? '' : '（我当前离线，正式 runtime 接入后真实执行）'}`,
                authorId: agent.id,
                authorKind: 'agent',
              })
              setMessages((prev) => [...prev, reply])
            } catch {
              /* ignore simulated-reply errors */
            }
          }, 1400)
        }
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setSending(false)
    }
  }, [activeChannel, text, authorId, authorKind, sending, agents])

  const insertMention = (handle: string) => {
    setText((t) => (t.endsWith(' ') || t === '' ? `${t}@${handle} ` : `${t} @${handle} `))
  }

  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const authorOptions = useMemo(
    () => [
      { group: 'Humans', items: humans.map((h) => ({ id: h.id, handle: h.name, kind: 'human' as MemberKind })) },
      { group: 'Agents', items: agents.map((a) => ({ id: a.id, handle: a.displayName, kind: 'agent' as MemberKind })) },
    ],
    [humans, agents],
  )

  if (loading) return <div className="loading">正在连接 Loop 控制面…</div>

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">◌</span> Loop
          <span className="brand-sub">可控投递协作</span>
        </div>
        {activeChannel && (
          <div className="channel-meta">
            <strong>{activeChannel.name}</strong>
            <span className="chip">{activeChannel.broadcastPolicy.defaultAudience === 'mentioned' ? '默认静默' : '默认全员'}</span>
            <span className={`chip ${activeChannel.broadcastPolicy.allowAtAll ? 'on' : 'off'}`}>@all {activeChannel.broadcastPolicy.allowAtAll ? '开' : '关'}</span>
            <span className={`chip ${activeChannel.broadcastPolicy.allowAtOnline ? 'on' : 'off'}`}>@online {activeChannel.broadcastPolicy.allowAtOnline ? '开' : '关'}</span>
          </div>
        )}
        <div className="actions">
          <button onClick={() => seed && loadChannel(activeChannel!, seed.workspaceId)}>刷新</button>
          <button onClick={bootstrap}>重置 demo</button>
        </div>
      </header>

      <div className="body">
        <aside className="sidebar">
          <div className="side-section">
            <h3>频道</h3>
            <button className="new-channel" onClick={createChannel}>+ 新频道</button>
            {channels.map((ch) => (
              <button
                key={ch.id}
                className={`channel-item ${ch.id === activeChannel?.id ? 'active' : ''}`}
                onClick={() => switchChannel(ch)}
              >
                <span className="hash">#</span>
                {ch.name.replace(/^#/, '')}
                <span className="count">{ch.memberCount}</span>
              </button>
            ))}
          </div>

          <div className="side-section">
            <h3>Agents</h3>
            {agents.map((a) => (
              <button key={a.id} className="member agent" onClick={() => insertMention(a.displayName)} title={a.description ?? ''}>
                <span className={`dot ${a.online ? 'online' : 'offline'}`} />
                <span className="name">{a.displayName}</span>
                <span className="role">{a.runtime}</span>
              </button>
            ))}
            <h3 className="mt">Humans</h3>
            {humans.map((h) => (
              <button key={h.id} className="member human" onClick={() => insertMention(h.name)}>
                <span className="dot online" />
                <span className="name">{h.name}</span>
              </button>
            ))}
            <p className="hint">点击成员插入 @mention</p>
          </div>
        </aside>

        <main className="stream">
          <div className="messages">
            {messages.length === 0 && (
              <div className="empty">还没有消息。试试 <button className="inline" onClick={() => insertMention('SpecBot')}>@SpecBot</button> 或 <strong>@all</strong>。</div>
            )}
            {messages.map((m) => (
              <MessageCard key={m.id} m={m} open={expanded.has(m.id)} onToggle={() => toggle(m.id)} />
            ))}
            <div ref={endRef} />
          </div>

          <div className="composer">
            <select
              value={`${authorKind}:${authorId}`}
              onChange={(e) => {
                const [kind, id] = e.target.value.split(':') as [MemberKind, string]
                setAuthorKind(kind)
                setAuthorId(id)
              }}
            >
              {authorOptions.map((grp) => (
                <optgroup key={grp.group} label={grp.group}>
                  {grp.items.map((o) => (
                    <option key={o.id} value={`${o.kind}:${o.id}`}>{o.handle}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            <textarea
              placeholder={activeChannel ? `发消息到 ${activeChannel.name} …  用 @SpecBot / @all / @online` : '选择一个频道'}
              value={text}
              onChange={(e) => setText(e.target.value)}
              disabled={!activeChannel || sending}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  void send()
                }
              }}
            />
            <button className="send" onClick={send} disabled={!text.trim() || sending}>
              {sending ? '…' : '发送'}
            </button>
          </div>
          {error && <div className="error">{error}</div>}
        </main>
      </div>
    </div>
  )
}

function MessageCard({ m, open, onToggle }: { m: MessageView; open: boolean; onToggle: () => void }) {
  const delivered = m.deliveries.filter((d) => d.state === 'delivered')
  const deferred = m.deliveries.filter((d) => d.state === 'deferred')
  const excluded = m.deliveries.filter((d) => d.state === 'excluded')
  const order: DeliveryView[] = [...delivered, ...deferred, ...excluded]
  const simulated = m.authorKind === 'agent'

  return (
    <div className={`message ${simulated ? 'simulated' : ''}`}>
      <div className="msg-head">
        <span className={`avatar ${m.authorKind}`}>{m.authorHandle.slice(0, 2)}</span>
        <span className="author">{m.authorHandle}</span>
        {simulated && <span className="tag">🤖 模拟</span>}
        <span className="time">{fmtTime(m.createdAt)}</span>
      </div>
      <div className="msg-body">{renderBody(m.body, m.mentions)}</div>

      {m.notices.length > 0 && (
        <div className="notices">
          {m.notices.map((n: NoticeView, i) => (
            <div key={i} className="notice">⚠ {n.detail}</div>
          ))}
        </div>
      )}

      <button className="diag-toggle" onClick={onToggle}>
        {open ? '▾' : '▸'} 投递诊断：{delivered.length} 投递 · {deferred.length} 延后 · {excluded.length} 未打扰
      </button>

      {open && (
        <div className="diagnostics">
          {order.map((d) => {
            const meta = STATE_META[d.state]
            return (
              <div key={d.id} className={`delivery ${meta.cls}`}>
                <span className="d-icon">{meta.icon}</span>
                <span className="d-handle">{d.recipientHandle}</span>
                <span className="d-state">{meta.label}</span>
                <span className="d-reason">{d.reasonCode}</span>
                {d.wake && <span className="d-wake">🔔 唤醒</span>}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
