// @mention tokenizer — PROTOCOL PARSING, not behavioral routing.
//
// This is the explicitly-carved-out "low-level protocol parsing" boundary in the
// CLAUDE.md agent/LLM hard-ban: like any chat client tokenizing @mentions, this
// produces STRUCTURED tokens (Mention[]) that downstream code reasons over.
// It does NOT decide who receives a message, and it does NOT match message bodies
// against keyword/rule tables. The decider (decider.ts) owns all delivery decisions
// and operates purely on these structured tokens + typed policy + membership facts.
//
// Handles resolve against a caller-supplied index built from channel membership, so
// an unknown @token is dropped rather than implicitly delivered.
//
// Keyword precedence (intentional): the bare tokens `@all` and `@online` are reserved
// broadcast keywords and take priority over member-handle resolution. A member whose
// handle is literally "all"/"online" is therefore un-mentionable — these handles are
// reserved by convention at the member-create boundary.

import type { Mention, MemberKind } from './types.ts'

const MENTION_RE = /@([^\s@,;:!>()\[\]{}"']+)/gu

export type HandleResolver = (
  handle: string,
) => { memberId: string; memberKind: MemberKind } | undefined

export function parseMentions(body: string, resolve: HandleResolver): Mention[] {
  const out: Mention[] = []
  const seen = new Set<string>()

  for (const match of body.matchAll(MENTION_RE)) {
    const handle = match[1]
    if (!handle) continue
    const lower = handle.toLowerCase()

    if (lower === 'all') {
      if (!seen.has('all')) {
        seen.add('all')
        out.push({ kind: 'all' })
      }
      continue
    }
    if (lower === 'online') {
      if (!seen.has('online')) {
        seen.add('online')
        out.push({ kind: 'online' })
      }
      continue
    }

    const resolved = resolve(handle)
    if (!resolved) continue // unknown handle -> dropped, never implicit

    const key = `${resolved.memberKind}:${resolved.memberId}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push({
      kind: 'member',
      memberId: resolved.memberId,
      memberKind: resolved.memberKind,
      handle,
    })
  }

  return out
}
