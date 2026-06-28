// Task service — parent/child work items (roadmap M3 §D.2.3 W7) + D-026 task<->dispatch combo.
//
// D-026 promotes task assignment to the 4th EXPLICIT wake: assignTask writes task.assignee
// under a CAS, synthesizes ONE assignment message authored by the seeded role="system" agent,
// and posts it with a broadcastPolicyOverride that locks the wake to "only the assignee".
// The synthesized message flows through the SAME postMessage -> decideDelivery -> spawnDispatch
// critical path (never a parallel direct dispatch), landing exactly one auditable
// message_delivery row with reason_code TASK_ASSIGNEE. cancelTask marks the task cancelled and
// kills its non-terminal dispatches (state dead) so a late complete hits state!=='claimed'
// (409, no duplicate reply). claimDispatch/completeDispatch auto-couple the task lifecycle.
//
// All access is workspace-scoped via a structured SQL equality (`id = ? AND workspace_id = ?`),
// never name/string matching. Status transitions are a CAS against the prior status so two
// concurrent PATCHes resolve to one winner (mirrors dispatch claim/complete). Cross-workspace
// references 404 (no existence leak), consistent with dispatch-service.

import type { Db } from '../db/client.ts'
import { newId, now } from '../db/id.ts'
import { postMessage } from './delivery-service.ts'
import type { MemberKind, TaskStatus, TaskView } from './contract.ts'
import { HttpError } from './dispatch-service.ts'
import { taskView, type RawTask } from './views.ts'

const TASK_COLS =
  'id, workspace_id, parent_task_id, thread_id, assignee_id, assignee_kind, title, status, assignment_message_id, created_at'

type CreateTaskInput = {
  title: string
  parentTaskId?: string | null
  threadId?: string | null
  assigneeId?: string | null
  assigneeKind?: MemberKind | null
}

function loadScopedTask(db: Db, id: string, workspaceId: string): RawTask {
  const t = db.get<RawTask>(
    `SELECT ${TASK_COLS} FROM task WHERE id = ? AND workspace_id = ?`,
    id,
    workspaceId,
  )
  if (!t) throw new HttpError(404, 'task not found')
  return t
}

// Validate cross-references structurally (SQL membership), scoped to the workspace. Each
// miss is a 404 that does not distinguish "wrong workspace" from "does not exist".
function assertThreadInWorkspace(db: Db, workspaceId: string, threadId: string): void {
  const th = db.get<{ ws: string }>(
    'SELECT ch.workspace_id AS ws FROM thread t JOIN channel ch ON ch.id = t.channel_id WHERE t.id = ?',
    threadId,
  )
  if (!th || th.ws !== workspaceId) throw new HttpError(404, 'thread not found')
}

function assertParentInWorkspace(db: Db, workspaceId: string, parentTaskId: string): void {
  const p = db.get<{ ws: string }>(
    'SELECT workspace_id AS ws FROM task WHERE id = ?',
    parentTaskId,
  )
  if (!p || p.ws !== workspaceId) throw new HttpError(404, 'parent task not found')
}

function assertAssigneeInWorkspace(
  db: Db,
  workspaceId: string,
  assigneeId: string,
  assigneeKind: MemberKind,
): void {
  const exists =
    assigneeKind === 'agent'
      ? db.get<{ id: string }>('SELECT id FROM agent WHERE id = ? AND workspace_id = ?', assigneeId, workspaceId)
      : db.get<{ id: string }>('SELECT id FROM human WHERE id = ? AND workspace_id = ?', assigneeId, workspaceId)
  if (!exists) throw new HttpError(404, `${assigneeKind} not found in workspace`)
}

export function createTask(db: Db, workspaceId: string, input: CreateTaskInput): TaskView {
  if (input.threadId) assertThreadInWorkspace(db, workspaceId, input.threadId)
  if (input.parentTaskId) assertParentInWorkspace(db, workspaceId, input.parentTaskId)
  if (input.assigneeId && input.assigneeKind) {
    assertAssigneeInWorkspace(db, workspaceId, input.assigneeId, input.assigneeKind)
  }
  const id = newId('tsk')
  db.run(
    'INSERT INTO task(id, workspace_id, parent_task_id, thread_id, assignee_id, assignee_kind, title, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)',
    id,
    workspaceId,
    input.parentTaskId ?? null,
    input.threadId ?? null,
    input.assigneeId ?? null,
    input.assigneeKind ?? null,
    input.title,
    'open',
    now(),
  )
  return taskView(db, db.get<RawTask>(`SELECT ${TASK_COLS} FROM task WHERE id = ?`, id)!)
}

export type ListTasksFilter = {
  threadId?: string
  // undefined = no parent filter; null = only roots; string = only direct children of that parent.
  parentTaskId?: string | null
}

// Flat list (M3 minimal): only direct children per parent. Deep recursion is a later slice.
export function listTasks(db: Db, workspaceId: string, filter: ListTasksFilter = {}): TaskView[] {
  let sql = `SELECT ${TASK_COLS} FROM task WHERE workspace_id = ?`
  const params: unknown[] = [workspaceId]
  if (filter.threadId !== undefined) {
    sql += ' AND thread_id = ?'
    params.push(filter.threadId)
  }
  if (filter.parentTaskId !== undefined) {
    if (filter.parentTaskId === null) sql += ' AND parent_task_id IS NULL'
    else {
      sql += ' AND parent_task_id = ?'
      params.push(filter.parentTaskId)
    }
  }
  sql += ' ORDER BY created_at'
  return db.all<RawTask>(sql, ...params).map((r) => taskView(db, r))
}

export function getTask(db: Db, workspaceId: string, taskId: string): TaskView {
  return taskView(db, loadScopedTask(db, taskId, workspaceId))
}

// Allowed status transitions. Reopen from a terminal state is intentionally not permitted
// (keeps the state machine minimal and auditable); extend the table to relax later.
//
// Directive: `done` is terminal for cancellation. A done task is closed by its dispatch
// completing (D-026 complete->done coupling); cancelling a done task would discard the
// completed work silently. Force-cancel of a done task is a SEPARATE admin path, NOT this
// slice — if you add it, do so as a privileged operator action with its own audit, not by
// relaxing this transition table (which would reopen the complete-beats-cancel race).
const ALLOWED_TRANSITIONS: Record<TaskStatus, TaskStatus[]> = {
  open: ['in_progress', 'done', 'cancelled'],
  in_progress: ['done', 'cancelled'],
  done: [],
  cancelled: [],
}

export function updateTaskStatus(
  db: Db,
  workspaceId: string,
  taskId: string,
  next: TaskStatus,
): TaskView {
  return db.transaction(() => {
    const t = loadScopedTask(db, taskId, workspaceId)
    const cur = t.status as TaskStatus
    if (cur === next) return taskView(db, t) // idempotent
    if (!ALLOWED_TRANSITIONS[cur]?.includes(next)) {
      throw new HttpError(409, `invalid task transition: ${cur} -> ${next}`)
    }
    // CAS against the prior status so a concurrent PATCH that already moved the state
    // resolves to zero changes here (409) rather than a lost update.
    const r = db.run(
      'UPDATE task SET status = ? WHERE id = ? AND workspace_id = ? AND status = ?',
      next,
      taskId,
      workspaceId,
      cur,
    )
    if (r.changes !== 1) throw new HttpError(409, 'task state changed concurrently')
    return taskView(db, loadScopedTask(db, taskId, workspaceId))
  })
}

// ---------- D-026: task<->dispatch combo (assignment as the 4th explicit wake) ----------

// Structured channel-membership fact (SQL equality), mirroring assertAssigneeInWorkspace.
// A non-member assignee is NEVER a delivery candidate (the decider only sees channel_member
// rows), so it would silently never wake — reject explicitly here. We do NOT auto-add the
// member: unaudited implicit channel fan-in is a moat smell (review finding HIGH).
function assertChannelMembership(
  db: Db,
  workspaceId: string,
  channelId: string,
  memberId: string,
  kind: MemberKind,
): void {
  // Scope the channel to the workspace first so a cross-workspace channel id yields 404
  // (no existence leak), consistent with the rest of the service.
  const ch = db.get<{ id: string }>(
    'SELECT id FROM channel WHERE id = ? AND workspace_id = ?',
    channelId,
    workspaceId,
  )
  if (!ch) throw new HttpError(404, 'channel not found')
  const m = db.get<{ member_id: string }>(
    'SELECT member_id FROM channel_member WHERE channel_id = ? AND member_id = ? AND member_kind = ?',
    channelId,
    memberId,
    kind,
  )
  if (!m) throw new HttpError(404, 'assignee is not a member of the channel')
}

// The seeded system ghost agent (role='system') authors synthesized assignment messages so
// message.author_kind stays closed at ('human','agent'). At most one per workspace.
function findSystemAgent(db: Db, workspaceId: string): string | null {
  const r = db.get<{ id: string }>(
    "SELECT id FROM agent WHERE workspace_id = ? AND role = 'system' LIMIT 1",
    workspaceId,
  )
  return r?.id ?? null
}

export type AssignTaskInput = {
  assigneeId: string
  channelId: string
}

// Assign a task: CAS the assignee, synthesize ONE assignment message authored by the system
// agent, and post it through the SAME critical path with a broadcastPolicyOverride locking
// the wake to "only the assignee" (via taskAssigneeIds). postMessage -> spawnDispatch writes
// dispatch.task_id in the same tx. Idempotent-CAS: double-assign throws 409 (so it cannot
// spawn a second dispatch). The synthesized body DELIBERATELY omits any @ token the resolver
// would map to the assignee, so the decider resolves the assignee at step 2.5 (TASK_ASSIGNEE),
// not step 3 (DIRECT_MENTION) — hard-ban clean (wake is driven by taskAssigneeIds, never body).
export function assignTask(
  db: Db,
  workspaceId: string,
  taskId: string,
  input: AssignTaskInput,
): TaskView {
  return db.transaction(() => {
    const task = loadScopedTask(db, taskId, workspaceId)
    assertAssigneeInWorkspace(db, workspaceId, input.assigneeId, 'agent')
    assertChannelMembership(db, workspaceId, input.channelId, input.assigneeId, 'agent')

    // CAS: only an unassigned, open task can be assigned. A double-assign or a non-open task
    // resolves to zero changes -> 409, so a second assignTask cannot spawn a second dispatch
    // or orphan a cancel query (review finding HIGH).
    const cas = db.run(
      "UPDATE task SET assignee_id = ?, assignee_kind = 'agent' WHERE id = ? AND workspace_id = ? AND assignee_id IS NULL AND status = 'open'",
      input.assigneeId,
      taskId,
      workspaceId,
    )
    if (cas.changes !== 1) throw new HttpError(409, 'task already assigned or not open')

    const systemAgentId = findSystemAgent(db, workspaceId)
    if (!systemAgentId) throw new HttpError(409, 'no system agent seeded')

    // Reload to read the now-set assignee + title for the synthesized body.
    const assigned = loadScopedTask(db, taskId, workspaceId)
    const assigneeDisplayName =
      db.get<{ display_name: string }>('SELECT display_name FROM agent WHERE id = ?', input.assigneeId)
        ?.display_name ?? 'agent'

    // CRITICAL (hard-ban): the body contains NO '@' sign at all. The assignee member token is
    // never mentioned, so parser.ts cannot yield a member mention -> step 3 DIRECT_MENTION is
    // unreachable for this message -> the assignee resolves ONLY at step 2.5 TASK_ASSIGNEE,
    // driven entirely by taskAssigneeIds below. Using a structured "[#tsk-<shortId>] <title>
    // — assigned to <name>" template keeps it human-readable while provably not parseable as a
    // member handle (no @ token). shortId = last 6 of the task id (audit-stable, non-secret).
    const shortId = task.id.slice(-6)
    const body = `[#tsk-${shortId}] ${task.title} — assigned to ${assigneeDisplayName}`

    const msg = postMessage(db, input.channelId, {
      body,
      authorId: systemAgentId,
      authorKind: 'agent',
      threadId: assigned.thread_id,
      // LOAD-BEARING (review finding HIGH): if the host channel policy is defaultAudience=
      // 'members', a plain assignment message would CHANNEL_BROADCAST-wake EVERY member agent.
      // This override locks the synthesized wake to "only @mentioned" = only the assignee (the
      // assignee is woken via taskAssigneeIds, not via a body mention). Ordinary members fall
      // to step 6 default and are EXCLUDED (quiet default), preserving the moat.
      broadcastPolicyOverride: { defaultAudience: 'mentioned', allowAtAll: false, allowAtOnline: false },
      contextScope: null,
      taskId: task.id,
      taskAssigneeIds: new Set([input.assigneeId]),
    })

    // Immutable audit projection: record the synthesized message id once (CAS so a re-assign
    // path cannot overwrite it). The dispatch already carries task_id for the lifecycle link.
    db.run(
      'UPDATE task SET assignment_message_id = ? WHERE id = ? AND assignment_message_id IS NULL',
      msg.id,
      taskId,
    )

    return taskView(db, loadScopedTask(db, taskId, workspaceId))
  })
}

// Cancel a task: CAS it to cancelled (only from a non-terminal state), then kill every
// related non-terminal dispatch (state -> dead). A dead dispatch's late complete hits
// state!=='claimed' -> 409 -> no postMessage -> NO duplicate reply (reuses the M3 lease-lost
// discard path; 'dead' is an existing enum, no new state). done is terminal for cancellation.
export function cancelTask(db: Db, workspaceId: string, taskId: string): TaskView {
  return db.transaction(() => {
    loadScopedTask(db, taskId, workspaceId)
    const cas = db.run(
      "UPDATE task SET status = 'cancelled' WHERE id = ? AND workspace_id = ? AND status IN ('open','in_progress')",
      taskId,
      workspaceId,
    )
    if (cas.changes !== 1) throw new HttpError(409, 'task is already terminal')

    db.run(
      "UPDATE dispatch SET state = 'dead' WHERE task_id = ? AND state IN ('pending','claimed')",
      taskId,
    )
    return taskView(db, loadScopedTask(db, taskId, workspaceId))
  })
}
