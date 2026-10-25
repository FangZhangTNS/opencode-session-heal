import { Plugin } from "@opencode/plugin"

import { spawnSync } from "node:child_process"
import { existsSync } from "node:fs"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

// One-shot healing for sessions that carry encrypted reasoning items issued to
// a different caller.
//
// Background: opencode replays reasoning parts holding metadata.openai as
// encrypted reasoning items. Those items are bound to the caller that created
// them, so after an API key change the upstream rejects the replay:
//
//   reasoning `encrypted_content` was not issued to this caller
//
// Workflow (half-automatic on purpose: swapping a key is a human decision):
//   1. detect the error, mark the session pending, log, stop
//   2. the user switches the key and restarts opencode
//   3. on startup, pending sessions are healed once: the encrypted continuation
//      fields are dropped, reasoning text is kept
//   4. a healed session is never processed again, new encrypted items keep working
//
// The plugin writes no files. State lives in plugin storage; the sqlite edit is
// done by scripts/session-heal-fix.py, which writes a row-level undo file first.

const HERE = dirname(fileURLToPath(import.meta.url))
const FIX_SCRIPT = join(HERE, "scripts", "session-heal-fix.py")
const STATE_KEY = "sessions"

const MARKERS = ["encrypted_content", "not issued to this caller", "invalid_encrypted_content"]

interface HealEntry {
  state: "pending" | "healed"
  cutAt: number
  markedAt: string
  healedAt: string | null
  error?: string
  removed?: number
}

type HealState = Record<string, HealEntry>

interface Python {
  cmd: string
  prefix: string[]
}

// Interpreters are probed at runtime: an absolute python path is machine
// specific and must never be shipped. SESSION_HEAL_PYTHON wins when set.
function resolvePython(): Python | null {
  const candidates: Python[] = []
  const override = process.env.SESSION_HEAL_PYTHON
  if (override) candidates.push({ cmd: override, prefix: [] })
  candidates.push(
    { cmd: "python3", prefix: [] },
    { cmd: "python", prefix: [] },
    { cmd: "py", prefix: ["-3"] },
  )
  for (const candidate of candidates) {
    const probe = spawnSync(candidate.cmd, [...candidate.prefix, "--version"], {
      encoding: "utf8",
      timeout: 5000,
    })
    if (probe.status === 0) return candidate
  }
  return null
}

// Reads message/name/stack/code/... defensively: V2 error envelopes differ per
// event type, and the marker may sit several levels deep.
function collectErrorText(error: unknown, depth = 0, seen = new Set<unknown>()): string {
  if (error == null || depth > 6) return ""
  if (typeof error === "string") return error
  if (typeof error !== "object") return String(error)
  if (seen.has(error)) return ""
  seen.add(error)
  const e = error as Record<string, unknown>
  const parts: string[] = []
  for (const k of ["message", "name", "stack", "code", "type", "statusCode", "responseBody", "error", "cause", "data"]) {
    if (e[k] !== undefined && e[k] !== null) parts.push(collectErrorText(e[k], depth + 1, seen))
  }
  if (parts.join(" ").trim().length < 20) {
    try {
      const j = JSON.stringify(error)
      if (j && j.length > 2 && j !== "{}") parts.push(j)
    } catch {}
  }
  return parts.join(" ")
}

async function loadState(ctx: { storage: { get(key: string): Promise<unknown> } }): Promise<HealState> {
  const raw = await ctx.storage.get(STATE_KEY)
  if (raw && typeof raw === "object") return raw as HealState
  return {}
}

async function saveState(
  ctx: { storage: { set(key: string, value: unknown): Promise<void> } },
  state: HealState,
): Promise<void> {
  await ctx.storage.set(STATE_KEY, state)
}

// Runs once per startup. A missing script or interpreter is reported and
// skipped: healing must never block opencode from starting.
async function healPending(ctx: {
  storage: { get(key: string): Promise<unknown>; set(key: string, value: unknown): Promise<void> }
}): Promise<void> {
  const state = await loadState(ctx)
  const pending = Object.entries(state).filter(([, v]) => v?.state === "pending")
  if (pending.length === 0) {
    console.info("[session-heal] no pending sessions.")
    return
  }
  if (!existsSync(FIX_SCRIPT)) {
    console.error(`[session-heal] fix script missing: ${FIX_SCRIPT}`)
    return
  }
  const python = resolvePython()
  if (!python) {
    console.error(
      "[session-heal] no Python 3 interpreter found. Set SESSION_HEAL_PYTHON to its path. " +
        `${pending.length} session(s) stay pending.`,
    )
    return
  }
  console.info(`[session-heal] healing ${pending.length} pending session(s).`)

  let changed = false
  for (const [sessionID, entry] of pending) {
    const result = spawnSync(
      python.cmd,
      [...python.prefix, FIX_SCRIPT, "--session", sessionID, "--cut-at", String(entry.cutAt)],
      { encoding: "utf8", timeout: 120000 },
    )
    const out = `${result.stdout ?? ""}${result.stderr ?? ""}`.trim().replace(/\s+/g, " ").slice(0, 400)
    if (result.status === 0 && out.startsWith("OK")) {
      entry.state = "healed"
      entry.healedAt = new Date().toISOString()
      const removed = out.match(/removed=(\d+)/)
      if (removed) entry.removed = Number(removed[1])
      changed = true
      console.info(`[session-heal] healed ${sessionID}: ${out}`)
    } else {
      console.error(`[session-heal] heal failed session=${sessionID} status=${result.status} out=${out}`)
    }
  }
  if (changed) await saveState(ctx, state)
}

export default Plugin.define({
  id: "session-heal",
  async setup(ctx) {
    try {
      await healPending(ctx)
    } catch (error) {
      console.error(`[session-heal] heal on startup failed: ${String(error).slice(0, 300)}`)
    }

    const controller = new AbortController()
    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          try {
            // V2 event envelope: { id, created, type, durable, location, data }.
            // These upstream 400s surface as session.step.failed /
            // session.execution.failed rather than session.error, so filter on
            // any error-shaped type and let MARKERS decide.
            const ev = event as {
              type?: string
              data?: Record<string, unknown>
              properties?: Record<string, unknown>
              durable?: { aggregateID?: string }
            }
            const type = typeof ev?.type === "string" ? ev.type : ""
            if (!/(error|failed)/i.test(type)) continue

            const props = (ev.data ?? ev.properties ?? {}) as Record<string, unknown>
            const sessionID =
              (typeof props["sessionID"] === "string" ? props["sessionID"] : "") ||
              (typeof props["sessionId"] === "string" ? props["sessionId"] : "") ||
              (typeof ev.durable?.aggregateID === "string" ? ev.durable.aggregateID : "")
            if (!sessionID) continue

            const text = collectErrorText(props["error"] ?? props).toLowerCase()
            const hit = MARKERS.find((marker) => text.includes(marker.toLowerCase()))
            if (!hit) continue

            const state = await loadState(ctx)
            const prev = state[sessionID]
            const cutAt = prev?.state === "pending" && typeof prev.cutAt === "number" ? prev.cutAt : Date.now()
            state[sessionID] = {
              state: "pending",
              cutAt,
              markedAt: prev?.markedAt ?? new Date().toISOString(),
              healedAt: null,
              error: hit,
            }
            await saveState(ctx, state)
            console.error(
              `[session-heal] marked session=${sessionID} event=${type} hit=${hit} cutAt=${cutAt}. ` +
                "Switch the API key and restart opencode; the heal runs once on the next start.",
            )
          } catch (error) {
            console.error(`[session-heal] event handler failed: ${String(error).slice(0, 300)}`)
          }
        }
      } catch {}
    })()

    return () => controller.abort()
  },
})
