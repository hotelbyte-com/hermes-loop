// Idempotent PM delivery scenario (D-015 MVP): workspace "hotelbyte" + #pm-delivery
// channel + 4 bots (Researcher/Spec/Design/Eng) + PM Alice. DesignBot is offline so
// @online demos DEFERRED_OFFLINE. Seeds 3 demo messages through the real decider so the
// diagnostics panel lights up immediately.

import type { Db } from '../db/client.ts'
import { newId, now } from '../db/id.ts'
import type { BroadcastPolicy, MemberKind } from '../delivery/types.ts'
import { postMessage } from '../api/delivery-service.ts'
import { hashToken } from '../api/dispatch-service.ts'
import { readMachineConfig, writeMachineConfig } from '../cli/machine-config.ts'

export type PmSeedResult = {
  workspaceId: string
  channelId: string
  aliceId: string
  machineId: string
  botAgentIds: Record<string, string>
  // The seeded system ghost agent (role='system') that authors synthesized task-assignment
  // messages (D-026). message.author_kind stays closed at ('human','agent') — the system
  // agent is an AGENT row, not a new author_kind enum value.
  systemAgentId: string
  demoMessages: number
}

type BotSpec = {
  display: string
  soul: string
  role: string
  desc: string
  runtime: string
  online: boolean
}

const BOTS: BotSpec[] = [
  { display: 'ResearcherBot', soul: 'PM Researcher', role: 'pm-researcher', desc: '调研 BRD：市场 / 竞品 / 用户', runtime: 'claude-code', online: true },
  { display: 'SpecBot', soul: 'Spec Writer', role: 'spec-writer', desc: '把调研整理成 PRD', runtime: 'claude-code', online: true },
  { display: 'DesignBot', soul: 'UX Designer', role: 'ux-designer', desc: '产出 UI/UX 方案', runtime: 'opencode', online: false },
  { display: 'EngBot', soul: 'Frontend Engineer', role: 'frontend-engineer', desc: '交付前端实现', runtime: 'claude-code', online: true },
]

export function seedPmScenario(db: Db): PmSeedResult {
  const slug = 'hotelbyte'
  const existing = db.get<{ id: string }>('SELECT id FROM workspace WHERE slug = ?', slug)

  const wid = existing?.id ?? createRow(
    db,
    'INSERT INTO workspace(id, slug, name, created_at) VALUES (?,?,?,?)',
    [newId('ws'), slug, 'Hotelbyte', now()],
  ).id

  const channelId = upsertChannel(db, wid)
  const machineId = upsertMachine(db, wid, 'alice-mbp')
  ensureMachineToken(db, machineId)
  const aliceId = upsertHuman(db, wid, 'Alice')
  upsertHuman(db, wid, 'Bob')

  const botAgentIds: Record<string, string> = {}
  for (const b of BOTS) {
    const soulId = upsertSoul(db, wid, b.soul, b.role, b.desc)
    const agentId = upsertAgent(db, wid, soulId, b.display)
    botAgentIds[b.display] = agentId
    upsertInstance(db, agentId, machineId, b.runtime, b.online)
    db.run(
      'INSERT OR IGNORE INTO channel_member(channel_id, member_id, member_kind) VALUES (?,?,?)',
      channelId, agentId, 'agent',
    )
  }

  // D-026: seed the system ghost agent (idempotent). It authors synthesized task-assignment
  // messages; it is an ordinary agent row with role='system' (NOT a member of any channel —
  // it never receives messages, only authors them). Not a runtime target: no instance.
  const systemSoulId = upsertSoul(db, wid, 'System', 'system', 'system ghost author for task assignment')
  const systemAgentId = upsertAgent(db, wid, systemSoulId, 'System', 'system')

  db.run(
    'INSERT OR IGNORE INTO channel_member(channel_id, member_id, member_kind, role) VALUES (?,?,?,?)',
    channelId, aliceId, 'human', 'owner',
  )

  // Demo messages — only when the channel has none yet (idempotent re-seed).
  const count = db.get<{ n: number }>(
    'SELECT COUNT(*) AS n FROM message WHERE channel_id = ?',
    channelId,
  )
  const already = (count?.n ?? 0) > 0
  let demoMessages = 0
  if (!already) {
    const post = (body: string) =>
      postMessage(db, channelId, {
        body,
        authorId: aliceId,
        authorKind: 'human' as MemberKind,
        threadId: null,
        broadcastPolicyOverride: null,
        contextScope: null,
      })
    post('@SpecBot 帮我把昨天的 BRD 调研整理成 PRD，今天下班前给我')
    post('@all 同步一下：本周要交付 v0.1，各角色对齐一下进度')
    post('@online 谁在线帮忙 review 一下刚出的 PRD？')
    demoMessages = 3
  }

  return { workspaceId: wid, channelId, aliceId, machineId, botAgentIds, systemAgentId, demoMessages }
}

// Provision an opaque bearer for the demo machine and persist it to .data/machine.json
// so `pnpm machine` can act as the runtime bridge out of the box. Reuses an existing
// valid token across re-seeds (so a running machine CLI is not invalidated); reissues
// only when the machine has no token or the local config file is gone/stale.
function ensureMachineToken(db: Db, machineId: string): void {
  const row = db.get<{ token_hash: string | null }>(
    'SELECT token_hash FROM machine WHERE id = ?',
    machineId,
  )
  const file = readMachineConfig()
  if (
    file &&
    file.machineId === machineId &&
    row?.token_hash &&
    row.token_hash === hashToken(file.token)
  ) {
    return // existing token still valid + on disk
  }
  const token = newId('mch')
  db.run(
    'UPDATE machine SET token_hash = ?, token_suffix = ? WHERE id = ?',
    hashToken(token),
    token.slice(-4),
    machineId,
  )
  writeMachineConfig({
    machineId,
    token,
    baseUrl: process.env.LOOP_BASE_URL ?? 'http://127.0.0.1:8188',
  })
}

function upsertChannel(db: Db, wid: string): string {
  const existing = db.get<{ id: string }>(
    'SELECT id FROM channel WHERE workspace_id = ? AND name = ?',
    wid, '#pm-delivery',
  )
  if (existing) return existing.id
  const policy: BroadcastPolicy = {
    defaultAudience: 'mentioned',
    allowAtAll: true,
    allowAtOnline: true,
  }
  return createRow(
    db,
    'INSERT INTO channel(id, workspace_id, name, kind, broadcast_policy, context_scope, created_at) VALUES (?,?,?,?,?,?,?)',
    [newId('ch'), wid, '#pm-delivery', 'channel', JSON.stringify(policy), 'channel', now()],
  ).id
}

function upsertMachine(db: Db, wid: string, name: string): string {
  const existing = db.get<{ id: string }>(
    'SELECT id FROM machine WHERE workspace_id = ? AND name = ?',
    wid, name,
  )
  return existing?.id ?? createRow(
    db,
    'INSERT INTO machine(id, workspace_id, name, owner, created_at) VALUES (?,?,?,?,?)',
    [newId('mch'), wid, name, 'alice', now()],
  ).id
}

function upsertHuman(db: Db, wid: string, name: string): string {
  const existing = db.get<{ id: string }>(
    'SELECT id FROM human WHERE workspace_id = ? AND name = ?',
    wid, name,
  )
  return existing?.id ?? createRow(
    db,
    'INSERT INTO human(id, workspace_id, name, created_at) VALUES (?,?,?,?)',
    [newId('hum'), wid, name, now()],
  ).id
}

function upsertSoul(db: Db, wid: string, name: string, role: string, desc: string): string {
  const existing = db.get<{ id: string }>(
    'SELECT id FROM soul WHERE workspace_id = ? AND name = ?',
    wid, name,
  )
  return existing?.id ?? createRow(
    db,
    'INSERT INTO soul(id, workspace_id, name, kind, role, description, created_at) VALUES (?,?,?,?,?,?,?)',
    [newId('soul'), wid, name, 'agent', role, desc, now()],
  ).id
}

// role defaults to 'member'; the D-026 system ghost author passes 'system'. On INSERT the
// role column is set explicitly (the schema default 'member' is the fallback for legacy rows).
function upsertAgent(db: Db, wid: string, soulId: string, display: string, role: 'member' | 'system' = 'member'): string {
  const existing = db.get<{ id: string }>(
    'SELECT id FROM agent WHERE workspace_id = ? AND display_name = ?',
    wid, display,
  )
  if (existing) {
    // Keep role consistent across re-seeds (a re-seed with a different role upgrades it).
    db.run('UPDATE agent SET role = ? WHERE id = ?', role, existing.id)
    return existing.id
  }
  return createRow(
    db,
    'INSERT INTO agent(id, workspace_id, soul_id, display_name, role, created_at) VALUES (?,?,?,?,?,?)',
    [newId('agt'), wid, soulId, display, role, now()],
  ).id
}

function upsertInstance(
  db: Db,
  agentId: string,
  machineId: string,
  runtime: string,
  online: boolean,
): void {
  const existing = db.get<{ id: string }>(
    'SELECT id FROM instance WHERE agent_id = ? AND machine_id = ? AND runtime = ?',
    agentId, machineId, runtime,
  )
  if (existing) {
    db.run('UPDATE instance SET online = ?, last_seen_at = ? WHERE id = ?', online ? 1 : 0, now(), existing.id)
    return
  }
  db.run(
    'INSERT INTO instance(id, agent_id, machine_id, runtime, online, last_seen_at, created_at) VALUES (?,?,?,?,?,?,?)',
    newId('inst'), agentId, machineId, runtime, online ? 1 : 0, now(), now(),
  )
}

function createRow(db: Db, sql: string, params: unknown[]): { id: string } {
  // All createRow callers pass the generated id as params[0].
  db.run(sql, ...params)
  return { id: params[0] as string }
}
