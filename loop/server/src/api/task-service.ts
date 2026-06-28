// Task service — parent/child work items (roadmap M3 §D.2.3 W7).
//
// The `task` table (db/schema.sql) was pre-created in M1/M2 but never wired. This is the
// minimal, decoupled wiring: tasks are lightweight work items (human- or agent-assigned),
// independent of the message-driven dispatch lifecycle — createTask/updateTaskStatus do NOT
// call postMessage and do NOT spawn dispatches. Composition with dispatch is a later slice.
//
// All access is workspace-scoped via a structured SQL equality (`id = ? AND workspace_id = ?`),
// never name/string matching. Status transitions are a CAS against the prior status so two
// concurrent PATCHes resolve to one winner (mirrors dispatch claim/complete). Cross-workspace
// references 404 (no existence leak), consistent with dispatch-service.

import type { Db } from '../db/client.ts'
import { newId, now } from '../db/id.ts'
import type { MemberKind, TaskStatus, TaskView } from './contract.ts'
import { HttpError } from './dispatch-service.ts'
import { taskView, type RawTask } from './views.ts'

const TASK_COLS =
  'id, workspace_id, parent_task_id, thread_id, assignee_id, assignee_kind, title, status, created_at'

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
// in M3 (keeps the state machine minimal and auditable); extend the table to relax later.
const ALLOWED_TRANSITIONS: Record<TaskStatus, TaskStatus[]> = {
  open: ['in_progress', 'done', 'cancelled'],
  in_progress: ['done', 'cancelled'],
  done: ['cancelled'],
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
