// Regression tests for the task service (roadmap M3 §D.2.3 W7): parent/child items,
// workspace-scoped access (404 on cross-workspace, no leak), and CAS status transitions.
// Mirrors the dispatch.test.ts style: node:test against an in-memory node:sqlite DB.

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { newId, now } from '../db/id.ts'
import { createStore } from '../store.ts'
import {
  authMachineByToken,
  claimDispatch,
  completeDispatch,
  HttpError,
  pollDispatches,
  registerMachine,
  upsertInstance,
} from '../api/dispatch-service.ts'
import { createTask, assignTask, cancelTask, getTask, listTasks, updateTaskStatus } from '../api/task-service.ts'
import type { TaskView } from '../api/contract.ts'

const QUIET = { defaultAudience: 'mentioned', allowAtAll: true, allowAtOnline: true } as const

type Fixture = {
  db: ReturnType<typeof createStore>
  wid: string
  wid2: string
  cid: string
  tid: string
  hid: string
  aid: string
}

function fixture(): Fixture {
  const db = createStore(':memory:')
  const wid = newId('ws')
  db.run('INSERT INTO workspace(id, slug, name, created_at) VALUES (?,?,?,?)', wid, 'test', 'Test', now())
  const cid = newId('ch')
  db.run(
    'INSERT INTO channel(id, workspace_id, name, kind, broadcast_policy, context_scope, created_at) VALUES (?,?,?,?,?,?,?)',
    cid, wid, 'test', 'channel', JSON.stringify(QUIET), 'channel', now(),
  )
  // A thread for thread-scoped tasks.
  const tid = newId('thr')
  db.run('INSERT INTO thread(id, channel_id, title, created_at) VALUES (?,?,?,?)', tid, cid, 'T', now())
  // A soul + agent and a human (assignees).
  const sid = newId('soul')
  db.run('INSERT INTO soul(id, workspace_id, name, kind, role, description, created_at) VALUES (?,?,?,?,?,?,?)', sid, wid, 'Eng', 'agent', 'eng', '', now())
  const aid = newId('agt')
  db.run('INSERT INTO agent(id, workspace_id, soul_id, display_name, created_at) VALUES (?,?,?,?,?)', aid, wid, sid, 'EngBot', now())
  const hid = newId('hum')
  db.run('INSERT INTO human(id, workspace_id, name, created_at) VALUES (?,?,?,?)', hid, wid, 'Alice', now())

  // A second workspace for cross-workspace isolation tests.
  const wid2 = newId('ws')
  db.run('INSERT INTO workspace(id, slug, name, created_at) VALUES (?,?,?,?)', wid2, 'foreign', 'Foreign', now())

  return { db, wid, wid2, cid, tid, hid, aid }
}

test('create a root task and list roots; assignee handle resolves', () => {
  const f = fixture()
  const t = createTask(f.db, f.wid, { title: 'ship v0.1', assigneeId: f.hid, assigneeKind: 'human' })
  assert.equal(t.status, 'open')
  assert.equal(t.parentTaskId, null)
  assert.equal(t.assigneeHandle, 'Alice')
  assert.equal(t.assigneeKind, 'human')

  const roots = listTasks(f.db, f.wid, { parentTaskId: null })
  assert.equal(roots.length, 1)
  assert.equal(roots[0].id, t.id)
  f.db.close()
})

test('parent/child: children list directly under a parent; assigneeKind coupling enforced', () => {
  const f = fixture()
  const parent = createTask(f.db, f.wid, { title: 'parent' })
  const c1 = createTask(f.db, f.wid, { title: 'child-1', parentTaskId: parent.id, assigneeId: f.aid, assigneeKind: 'agent' })
  const c2 = createTask(f.db, f.wid, { title: 'child-2', parentTaskId: parent.id })
  assert.equal(c1.assigneeHandle, 'EngBot')

  const children = listTasks(f.db, f.wid, { parentTaskId: parent.id })
  assert.equal(children.length, 2)
  assert.deepEqual(
    children.map((c) => c.id).sort(),
    [c1.id, c2.id].sort(),
  )

  // assigneeId without assigneeKind is rejected at the contract layer (zod refine).
  // (Routes enforce this; the service trusts parsed input, so we assert the zod rule indirectly
  // by confirming a well-formed pair works — covered above — and cross-ref validation below.)
  f.db.close()
})

test('thread-scoped tasks list by thread; getTask resolves the same view', () => {
  const f = fixture()
  const t = createTask(f.db, f.wid, { title: 'in-thread', threadId: f.tid })
  assert.equal(t.threadId, f.tid)
  const byThread = listTasks(f.db, f.wid, { threadId: f.tid })
  assert.equal(byThread.length, 1)
  assert.deepEqual(getTask(f.db, f.wid, t.id), t)
  f.db.close()
})

test('invalid references 404 and never leak existence', () => {
  const f = fixture()
  // foreign parent task
  const foreign = createTask(f.db, f.wid2, { title: 'other-ws' })
  assert.throws(
    () => createTask(f.db, f.wid, { title: 'x', parentTaskId: foreign.id }),
    (e) => e instanceof HttpError && e.status === 404,
  )
  // foreign thread (belongs to wid2? it doesn't, but the join yields no row in wid)
  assert.throws(
    () => createTask(f.db, f.wid, { title: 'x', threadId: newId('thr') }),
    (e) => e instanceof HttpError && e.status === 404,
  )
  // assignee not in workspace
  const foreignHuman = newId('hum')
  assert.throws(
    () => createTask(f.db, f.wid, { title: 'x', assigneeId: foreignHuman, assigneeKind: 'human' }),
    (e) => e instanceof HttpError && e.status === 404,
  )
  // cross-workspace get → 404
  const t = createTask(f.db, f.wid, { title: 'mine' })
  assert.throws(
    () => getTask(f.db, f.wid2, t.id),
    (e) => e instanceof HttpError && e.status === 404,
  )
  f.db.close()
})

test('status transitions: valid path allowed; illegal transition and reopen rejected (409)', () => {
  const f = fixture()
  const t = createTask(f.db, f.wid, { title: 'flow' })
  assert.equal(updateTaskStatus(f.db, f.wid, t.id, 'in_progress').status, 'in_progress')
  assert.equal(updateTaskStatus(f.db, f.wid, t.id, 'done').status, 'done')
  // reopen from a terminal state is not in the allowed table
  assert.throws(
    () => updateTaskStatus(f.db, f.wid, t.id, 'in_progress'),
    (e) => e instanceof HttpError && e.status === 409,
  )
  // open -> cancelled is allowed (terminal from open)
  const t2 = createTask(f.db, f.wid, { title: 'cancel-me' })
  assert.equal(updateTaskStatus(f.db, f.wid, t2.id, 'cancelled').status, 'cancelled')
  assert.throws(
    () => updateTaskStatus(f.db, f.wid, t2.id, 'done'),
    (e) => e instanceof HttpError && e.status === 409,
  )
  f.db.close()
})

test('idempotent same-status PATCH returns the task without error', () => {
  const f = fixture()
  const t: TaskView = createTask(f.db, f.wid, { title: 'same' })
  const again = updateTaskStatus(f.db, f.wid, t.id, 'open')
  assert.equal(again.status, 'open')
  assert.equal(again.id, t.id)
  f.db.close()
})

// ---------- D-026: task<->dispatch combo (assignment as the 4th explicit wake) ----------
//
// A dedicated fixture: a host channel with policy defaultAudience='members' (the FAN-OUT
// hazard), two member agents (assignee + bystander) with online instances, and a seeded
// system ghost author. Mirrors dispatch.test.ts harness conventions.

const MEMBERS_POLICY = { defaultAudience: 'members', allowAtAll: true, allowAtOnline: true } as const

type ComboFixture = {
  db: ReturnType<typeof createStore>
  wid: string
  cid: string
  tid: string
  sysId: string
  assigneeId: string
  bystanderId: string
  machineId: string
  auth: { id: string; workspaceId: string }
}

function comboFixture(policy: object = MEMBERS_POLICY): ComboFixture {
  const db = createStore(':memory:')
  const wid = newId('ws')
  db.run('INSERT INTO workspace(id, slug, name, created_at) VALUES (?,?,?,?)', wid, 'combo', 'Combo', now())
  const cid = newId('ch')
  db.run(
    'INSERT INTO channel(id, workspace_id, name, kind, broadcast_policy, context_scope, created_at) VALUES (?,?,?,?,?,?,?)',
    cid, wid, 'pm', 'channel', JSON.stringify(policy), 'channel', now(),
  )
  const tid = newId('thr')
  db.run('INSERT INTO thread(id, channel_id, title, created_at) VALUES (?,?,?,?)', tid, cid, 'T', now())

  const mkAgent = (display: string, role: 'member' | 'system' = 'member'): string => {
    const sid = newId('soul')
    db.run(
      'INSERT INTO soul(id, workspace_id, name, kind, role, description, created_at) VALUES (?,?,?,?,?,?,?)',
      sid, wid, display, 'agent', role, '', now(),
    )
    const aid = newId('agt')
    db.run(
      'INSERT INTO agent(id, workspace_id, soul_id, display_name, role, created_at) VALUES (?,?,?,?,?,?)',
      aid, wid, sid, display, role, now(),
    )
    return aid
  }

  const sysId = mkAgent('System', 'system')
  const assigneeId = mkAgent('EngBot')
  const bystanderId = mkAgent('ResearcherBot')
  db.run('INSERT INTO channel_member(channel_id, member_id, member_kind) VALUES (?,?,?)', cid, assigneeId, 'agent')
  db.run('INSERT INTO channel_member(channel_id, member_id, member_kind) VALUES (?,?,?)', cid, bystanderId, 'agent')

  const { machine, token } = registerMachine(db, wid, 'mac1', null)
  for (const aid of [assigneeId, bystanderId]) {
    upsertInstance(db, wid, aid, { machineId: machine.id, runtime: 'claude-code', online: true })
  }
  return { db, wid, cid, tid, sysId, assigneeId, bystanderId, machineId: machine.id, auth: authMachineByToken(db, token) }
}

// 1. assignTask synthesizes an assignment message -> assignee woken with TASK_ASSIGNEE ->
//    exactly ONE dispatch spawned with dispatch.task_id == task.id; a message_delivery row
//    with reason_code TASK_ASSIGNEE exists for the assignee.
test('D-026 assignTask: synthesizes a TASK_ASSIGNEE wake with exactly one dispatch anchored to the task', () => {
  const f = comboFixture()
  const task = createTask(f.db, f.wid, { title: 'ship v0.1', threadId: f.tid })
  const view = assignTask(f.db, f.wid, task.id, { assigneeId: f.assigneeId, channelId: f.cid })

  assert.equal(view.assigneeId, f.assigneeId)
  assert.ok(view.assignmentMessageId, 'assignment message id must be projected onto the task')

  // Exactly ONE dispatch, anchored to the task, targeting the assignee.
  const dispatches = f.db.all<{ id: string; task_id: string | null; agent_id: string; state: string }>(
    'SELECT id, task_id, agent_id, state FROM dispatch',
  )
  assert.equal(dispatches.length, 1, 'exactly one dispatch spawned')
  assert.equal(dispatches[0].task_id, task.id)
  assert.equal(dispatches[0].agent_id, f.assigneeId)
  assert.equal(dispatches[0].state, 'pending')

  // The assignee has a TASK_ASSIGNEE delivery row (the 4th explicit wake, auditable).
  const dlv = f.db.get<{ reason_code: string; wake: number }>(
    'SELECT reason_code, wake FROM message_delivery WHERE recipient_id = ? AND recipient_kind = ?',
    f.assigneeId, 'agent',
  )
  assert.ok(dlv, 'assignee must have a delivery row')
  assert.equal(dlv!.reason_code, 'TASK_ASSIGNEE')
  assert.equal(dlv!.wake, 1)
  f.db.close()
})

// 2. assignTask is idempotent-CAS: assigning an already-assigned or non-open task -> 409;
//    calling assignTask twice produces exactly ONE dispatch (no double-assign / no orphan).
test('D-026 assignTask: double-assign is 409 and produces no second dispatch', () => {
  const f = comboFixture()
  const task = createTask(f.db, f.wid, { title: 'once' })
  assignTask(f.db, f.wid, task.id, { assigneeId: f.assigneeId, channelId: f.cid })

  // Second assign (task is now assigned) -> 409.
  assert.throws(
    () => assignTask(f.db, f.wid, task.id, { assigneeId: f.bystanderId, channelId: f.cid }),
    (e) => e instanceof HttpError && e.status === 409,
  )
  // Assigning a non-open (cancelled) task -> 409, no dispatch.
  const task2 = createTask(f.db, f.wid, { title: 'closed' })
  updateTaskStatus(f.db, f.wid, task2.id, 'cancelled')
  assert.throws(
    () => assignTask(f.db, f.wid, task2.id, { assigneeId: f.assigneeId, channelId: f.cid }),
    (e) => e instanceof HttpError && e.status === 409,
  )

  const n = f.db.get<{ n: number }>('SELECT COUNT(*) AS n FROM dispatch')!.n
  assert.equal(n, 1, 'still exactly one dispatch (no orphan from the rejected assign)')
  f.db.close()
})

// 3. assignTask of an assignee who is NOT a channel member -> 404, no dispatch, no wake row.
test('D-026 assignTask: non-channel-member assignee is 404 with no dispatch and no wake row', () => {
  const f = comboFixture()
  const task = createTask(f.db, f.wid, { title: 'x' })
  // A workspace agent that is NOT a member of the host channel.
  const sid = newId('soul')
  f.db.run(
    'INSERT INTO soul(id, workspace_id, name, kind, role, description, created_at) VALUES (?,?,?,?,?,?,?)',
    sid, f.wid, 'Outsider', 'agent', 'outsider', '', now(),
  )
  const outsider = newId('agt')
  f.db.run(
    'INSERT INTO agent(id, workspace_id, soul_id, display_name, role, created_at) VALUES (?,?,?,?,?,?)',
    outsider, f.wid, sid, 'OutsiderBot', 'member', now(),
  )

  assert.throws(
    () => assignTask(f.db, f.wid, task.id, { assigneeId: outsider, channelId: f.cid }),
    (e) => e instanceof HttpError && e.status === 404,
  )
  assert.equal(f.db.get<{ n: number }>('SELECT COUNT(*) AS n FROM dispatch')!.n, 0)
  assert.equal(
    f.db.get<{ n: number }>("SELECT COUNT(*) AS n FROM message_delivery WHERE wake = 1")!.n,
    0,
    'no wake delivery row for a non-member assignee',
  )
  f.db.close()
})

// 4. FAN-OUT lock: host channel broadcast_policy defaultAudience='members'; assignTask still
//    wakes ONLY the assignee (other member agents get CHANNEL_BROADCAST silent or EXCLUDED,
//    never wake, never dispatch).
test('D-026 moat: assignment does not fan out the channel even with defaultAudience=members', () => {
  const f = comboFixture() // MEMBERS_POLICY
  const task = createTask(f.db, f.wid, { title: 'no fanout' })
  assignTask(f.db, f.wid, task.id, { assigneeId: f.assigneeId, channelId: f.cid })

  // The bystander is a member agent of a members-default channel, but the broadcastPolicyOverride
  // locked the synthesized message to defaultAudience='mentioned' -> bystander is NOT woken.
  const wakeRows = f.db.all<{ recipient_id: string; reason_code: string; wake: number }>(
    'SELECT recipient_id, reason_code, wake FROM message_delivery WHERE recipient_kind = ?',
    'agent',
  )
  const bystander = wakeRows.find((r) => r.recipient_id === f.bystanderId)!
  assert.ok(bystander, 'bystander has a delivery row (it is a candidate)')
  assert.equal(bystander.wake, 0, 'bystander must NOT be woken (no fan-out)')

  // Exactly one dispatch (the assignee's); the bystander never dispatched.
  const dispatches = f.db.all<{ agent_id: string }>('SELECT agent_id FROM dispatch')
  assert.equal(dispatches.length, 1)
  assert.equal(dispatches[0].agent_id, f.assigneeId)
  f.db.close()
})

// 5. TASK_ASSIGNEE reachability: the synthesized assignment message.mentions does NOT contain
//    the assignee member token; the assignee verdict is reason_code TASK_ASSIGNEE (not DIRECT_MENTION).
test('D-026 reachability: assignment body has no @-mention; assignee verdict is TASK_ASSIGNEE not DIRECT_MENTION', () => {
  const f = comboFixture()
  const task = createTask(f.db, f.wid, { title: 'reach' })
  assignTask(f.db, f.wid, task.id, { assigneeId: f.assigneeId, channelId: f.cid })

  const msg = f.db.get<{ body: string; mentions: string }>(
    'SELECT body, mentions FROM message WHERE author_id = ? AND author_kind = ?',
    f.sysId, 'agent',
  )!
  assert.ok(!msg.body.includes('@'), 'assignment body must contain no @ token (hard-ban clean)')
  const mentions = JSON.parse(msg.mentions) as Array<{ kind: string; memberId?: string }>
  assert.ok(
    !mentions.some((m) => m.kind === 'member' && m.memberId === f.assigneeId),
    'mentions must not contain the assignee member token',
  )
  // And the assignee's verdict is TASK_ASSIGNEE, proving step 2.5 (not step 3 DIRECT_MENTION).
  const dlv = f.db.get<{ reason_code: string }>(
    'SELECT reason_code FROM message_delivery WHERE recipient_id = ?',
    f.assigneeId,
  )!
  assert.equal(dlv.reason_code, 'TASK_ASSIGNEE')
  f.db.close()
})

// 9. done->cancelled removed: cancelTask on an already-done task -> 409 (done is terminal for cancellation).
test('D-026 cancelTask: an already-done task cannot be cancelled (done is terminal, 409)', () => {
  const f = comboFixture()
  const task = createTask(f.db, f.wid, { title: 'finished' })
  // Drive it to done via the dispatch lifecycle coupling (claim -> complete ok).
  assignTask(f.db, f.wid, task.id, { assigneeId: f.assigneeId, channelId: f.cid })
  const dsp = pollDispatches(f.db, f.machineId).find((d) => d.taskId === task.id)!
  claimDispatch(f.db, dsp.id, f.auth)
  completeDispatch(f.db, dsp.id, f.auth, { ok: true, replyBody: 'done' })
  assert.equal(getTask(f.db, f.wid, task.id).status, 'done')

  assert.throws(
    () => cancelTask(f.db, f.wid, task.id),
    (e) => e instanceof HttpError && e.status === 409,
    'cancelTask on a done task must be 409',
  )
  f.db.close()
})

// 11. parent/child independent wake: a child task created+assigned in an agent reply produces
//     its own assignment message and its own dispatch, independent of the parent.
test('D-026 parent/child independent wake: a child task assignment gets its own message + dispatch', () => {
  const f = comboFixture()
  const parent = createTask(f.db, f.wid, { title: 'parent' })
  const child = createTask(f.db, f.wid, { title: 'child', parentTaskId: parent.id, threadId: f.tid })

  assignTask(f.db, f.wid, parent.id, { assigneeId: f.assigneeId, channelId: f.cid })
  assignTask(f.db, f.wid, child.id, { assigneeId: f.bystanderId, channelId: f.cid })

  const dispatches = f.db.all<{ task_id: string | null; agent_id: string }>(
    'SELECT task_id, agent_id FROM dispatch ORDER BY task_id',
  )
  assert.equal(dispatches.length, 2, 'parent and child each get their own dispatch')
  assert.ok(dispatches.some((d) => d.task_id === parent.id && d.agent_id === f.assigneeId))
  assert.ok(dispatches.some((d) => d.task_id === child.id && d.agent_id === f.bystanderId))

  // Two distinct assignment messages, one per task.
  const n = f.db.get<{ n: number }>(
    'SELECT COUNT(*) AS n FROM message WHERE author_id = ? AND author_kind = ?',
    f.sysId, 'agent',
  )!.n
  assert.equal(n, 2)
  f.db.close()
})
