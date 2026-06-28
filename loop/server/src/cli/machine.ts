// `pnpm machine` — reference runtime bridge client (D-024).
//
// Reads the machine config written by `pnpm seed` (.data/machine.json), polls the
// control plane for dispatches targeting agents hosted on this machine, claims each,
// runs a runtime, and completes the dispatch — posting the runtime's output back as a
// real agent message through the server.
//
// The server NEVER executes a runtime (D-021); THIS process does. By default it uses a
// built-in stub reply so the bridge is demonstrable with zero deps. Attach a real
// runtime with `--exec <cmd...>`: the command receives the dispatch payload as JSON on
// stdin and its stdout becomes the agent's reply.
//
//   pnpm machine                       # built-in stub replies
//   pnpm machine --exec claude -p      # real runtime: claude reads payload on stdin
//   pnpm machine --exec sh -c 'echo ...'
//
// Options: --interval <ms>  (poll period, default 1500)

import { spawn } from 'node:child_process'

import type { DispatchView } from '../api/contract.ts'
import { readMachineConfig } from './machine-config.ts'

type RuntimeInput = {
  dispatchId: string
  agentId: string
  agentHandle: string
  channelId: string
  threadId: string | null
  body: string
  authorHandle: string
  reasonCode: string
  createdAt: number
}

type RuntimeResult = { ok: boolean; replyBody?: string; error?: string }

function parseArgs(argv: string[]): { interval: number; exec: string[] | null } {
  let interval = 1500
  let exec: string[] | null = null
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--interval') interval = Number(argv[++i]) || 1500
    else if (a === '--exec') exec = argv.slice(i + 1)
  }
  return { interval, exec }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

// Lease renewal interval: comfortably under half the server's LOOP_CLAIM_TTL_MS so a
// long-running runtime keeps its claim and is not requeued onto another machine
// (which would duplicate the agent reply). Defaults to 60s when TTL is the 5min default.
function renewIntervalMs(): number {
  const ttl = Number(process.env.LOOP_CLAIM_TTL_MS)
  const t = Number.isFinite(ttl) && ttl > 0 ? ttl : 300_000
  return Math.min(Math.floor(t / 2), 60_000)
}

async function execute(d: DispatchView, exec: string[] | null): Promise<RuntimeResult> {
  const input: RuntimeInput = {
    dispatchId: d.id,
    agentId: d.agentId,
    agentHandle: d.agentHandle,
    channelId: d.channelId,
    threadId: d.threadId,
    body: d.payload.body,
    authorHandle: d.payload.authorHandle,
    reasonCode: d.payload.reasonCode,
    createdAt: d.payload.createdAt,
  }

  if (exec) {
    return runExternal(exec, input)
  }
  return stubReply(input)
}

function runExternal(exec: string[], input: RuntimeInput): Promise<RuntimeResult> {
  return new Promise((resolve) => {
    const child = spawn(exec[0], exec.slice(1), { stdio: ['pipe', 'pipe', 'pipe'] })
    let out = ''
    let err = ''
    child.stdout.on('data', (d) => (out += d))
    child.stderr.on('data', (d) => (err += d))
    child.on('error', (e) => resolve({ ok: false, error: `spawn failed: ${String(e)}` }))
    child.on('close', (code) => {
      const body = out.trim()
      if (code === 0 && body) resolve({ ok: true, replyBody: body })
      else resolve({ ok: false, error: `exit ${code}${err ? `: ${err.trim().slice(0, 400)}` : ''}` })
    })
    child.stdin.end(JSON.stringify(input))
  })
}

function stubReply(input: RuntimeInput): RuntimeResult {
  // NOTE: we deliberately do NOT echo the triggering message body. The decider parses
  // @mentions in EVERY message (an agent reply is no exception — that uniform routing
  // IS the moat). A body echo containing @all / @online / @Agent would re-broadcast and
  // cascade. A real runtime owns its output; the stub keeps it mention-light and simply
  // addresses the original author.
  return {
    ok: true,
    replyBody:
      `⟐ [runtime-bridge stub] 收到「${input.reasonCode}」唤醒（来自 @${input.authorHandle}）。` +
      `已通过 dispatch→claim→complete 桥真实回写。\n` +
      `接真实 runtime：\`pnpm machine --exec <cmd>\`（stdin 收 payload JSON，stdout 即回复）。`,
  }
}

async function handleOne(
  base: string,
  headers: Record<string, string>,
  d: DispatchView,
  exec: string[] | null,
): Promise<void> {
  const claim = await fetch(`${base}/api/dispatches/${d.id}/claim`, { method: 'POST', headers })
  if (!claim.ok) return // raced or no longer eligible — silently move on
  const claimed = (await claim.json()) as DispatchView
  console.log(
    `[loop-machine] claimed ${d.id} → ${claimed.agentHandle} (${claimed.payload.reasonCode})`,
  )

  // Renew the lease WHILE the runtime executes (M3, D-024 Directive). Without this, a
  // runtime exceeding LOOP_CLAIM_TTL_MS gets its dispatch requeued by the server, another
  // machine re-claims and re-runs it → a DUPLICATE agent reply. If the server reports the
  // lease lost (non-2xx: it was requeued/re-claimed), stop renewing and let the runtime
  // finish naturally — its `complete` will then 409 and be discarded below. We do NOT kill
  // the spawned child (no handle to it here): the moat is preserved (no duplicate reply),
  // only the orphaned runtime's work is wasted. A transient network error keeps retrying.
  const renewMs = renewIntervalMs()
  let leaseLost = false
  const renewTimer = setInterval(async () => {
    try {
      const r = await fetch(`${base}/api/dispatches/${d.id}/renew`, { method: 'POST', headers })
      if (!r.ok) {
        leaseLost = true
        clearInterval(renewTimer)
        console.warn(
          `[loop-machine] lease lost on ${d.id} (renew ${r.status}); result will be discarded`,
        )
      }
    } catch (e) {
      console.warn(`[loop-machine] renew ${d.id} failed (will retry): ${String(e)}`)
    }
  }, renewMs)

  let result: RuntimeResult
  try {
    result = await execute(claimed, exec)
  } finally {
    clearInterval(renewTimer)
  }

  const complete = await fetch(`${base}/api/dispatches/${d.id}/complete`, {
    method: 'POST',
    headers,
    body: JSON.stringify(result),
  })
  if (complete.ok) {
    const body = (await complete.json()) as { reply?: { id: string } | null }
    console.log(
      `[loop-machine] completed ${d.id} ok=${result.ok} reply=${body.reply ? body.reply.id : '—'}`,
    )
  } else if (leaseLost) {
    console.warn(`[loop-machine] complete ${d.id} discarded (lease had been lost): ${complete.status}`)
  } else {
    console.error(`[loop-machine] complete failed: ${complete.status} ${await complete.text()}`)
  }
}

async function main(): Promise<void> {
  const { interval, exec } = parseArgs(process.argv.slice(2))
  const cfg = readMachineConfig()
  if (!cfg) {
    console.error('[loop-machine] no machine config found. Run `pnpm seed` first.')
    process.exit(1)
  }
  const base = cfg.baseUrl.replace(/\/$/, '')
  const headers = {
    authorization: `Bearer ${cfg.token}`,
    'content-type': 'application/json',
  }
  console.log(
    `[loop-machine] polling ${base} as ${cfg.machineId} every ${interval}ms ` +
      `(runtime: ${exec ? exec.join(' ') : 'built-in stub'})`,
  )

  // Poll loop. Exit on SIGINT for clean shutdown.
  let running = true
  const stop = () => (running = false)
  process.on('SIGINT', stop)
  process.on('SIGTERM', stop)

  while (running) {
    try {
      const res = await fetch(`${base}/api/machines/${cfg.machineId}/dispatches?limit=4`, {
        headers,
      })
      if (res.status === 401) {
        console.error('[loop-machine] token rejected (401) — re-run `pnpm seed` and restart.')
        process.exit(1)
      }
      if (res.ok) {
        const dispatches = (await res.json()) as DispatchView[]
        for (const d of dispatches) await handleOne(base, headers, d, exec)
      } else {
        console.error(`[loop-machine] poll failed: ${res.status}`)
      }
    } catch (e) {
      console.error(`[loop-machine] error: ${String(e)}`)
    }
    await sleep(interval)
  }
}

void main()
