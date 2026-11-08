// aistatus-led :: DeepSeek Harness (DSH) plugin
// Forwards DSH's real event-bus events to the local LED middleware (ai-status/1).
//
// Event names were extracted from "DeepSeek Harness/resources/app.asar" and are
// kept in sync with profiles/dsh.json.
//
// IMPORTANT: "turn/end" is NOT a Cordis event. DSH writes it via
//   this.session.append("turn/end", ...)
// and emits it as a "session/event" whose payload has type === "turn/end".
// So we subscribe to session/event and re-dispatch by its `type`.
// "agent/turn-stopping" is a real ctx event and is used as a fallback.
//
// Several DSH events are Cordis "waterfall" events (request, next). We always
// call next() so we never block DSH's own approval / tool flow.
import { appendFileSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'

const DEBUG = process.env.AISTATUS_DEBUG === '1'
const LOG_FILE = join(homedir(), '.dsh', 'aistatus-led.log')
const VERSION = '1.0.2'

function log(line) {
  if (!DEBUG) return
  try {
    appendFileSync(LOG_FILE, new Date().toISOString() + ' ' + line + '\n', 'utf8')
  } catch (_) { /* logging must never break DSH */ }
}

function makePost(config) {
  const port = Number(config?.port) || Number(process.env.AISTATUS_PORT) || 8765
  const endpoint = `http://127.0.0.1:${port}/event`
  return (payload) => {
    let body
    try {
      body = JSON.stringify({ _profile: 'dsh', ...payload })
    } catch (_) {
      // DSH objects can be cyclic; fall back to event name only
      body = JSON.stringify({ _profile: 'dsh', hook_event_name: payload.hook_event_name })
    }
    try {
      const p = fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
      })
      if (p && typeof p.catch === 'function') p.catch((e) => log('POST failed: ' + e))
    } catch (e) {
      log('POST threw: ' + e)
      // middleware not running: silently ignore, never affect DSH
    }
  }
}

function toolName(x) {
  if (!x || typeof x !== 'object') return ''
  return x.tool?.name || x.name || x.toolName || x.tool?.id ||
         x.tool_name || x.function?.name || ''
}

function toolInput(x) {
  if (!x || typeof x !== 'object') return {}
  return x.args || x.input || x.toolInput || x.parameters ||
         x.arguments || x.function?.arguments || {}
}

function approvalResult(args) {
  for (const a of args) {
    if (typeof a === 'string') return a
    if (a && typeof a === 'object') {
      for (const k of ['decision', 'result', 'status', 'action', 'outcome', 'value']) {
        if (typeof a[k] === 'string') return a[k]
      }
    }
  }
  return ''
}

const PRE_TOOL = new Set(['tools/pre-execute', 'tools/call', 'tool/call', 'tools/execute'])
const POST_TOOL = new Set(['tools/post-execute', 'tools/result', 'tool/result'])
// session/event carries the real turn lifecycle in payload.type
const SESSION_EVENT_TYPES = new Set(['turn/start', 'turn/end'])
// high-frequency stream events: send at most once per 300ms (state stays the same anyway)
const THROTTLED = new Set([
  'agent/assistant-stream', 'assistant/chunk', 'assistant/live-chunk', 'assistant/attempt',
])
const THROTTLE_MS = 300

function normalize(event, args) {
  if (event === 'session/event') {
    let type = ''
    for (const a of args) {
      if (a && typeof a === 'object' && typeof a.type === 'string') { type = a.type; break }
    }
    if (!SESSION_EVENT_TYPES.has(type)) return null
    return { hook_event_name: type, session_event_type: type }
  }

  const out = { hook_event_name: event }
  const first = args[0]
  const second = args[1]
  if (PRE_TOOL.has(event)) {
    const name = toolName(first) || toolName(second)
    if (name) out.tool_name = name
    const input = toolInput(first) || toolInput(second)
    if (input && Object.keys(input).length) out.tool_input = input
  }
  if (POST_TOOL.has(event)) {
    const name = toolName(first) || toolName(second)
    if (name) out.tool_name = name
    const resp = second !== undefined ? second : first
    if (resp !== undefined) out.tool_response = resp
  }
  if (event === 'approval/decided' || event === 'approval/asked') {
    out.approval_result = approvalResult(args)
  }
  return out
}

export const name = 'aistatus-led'

export function apply(ctx, config = {}) {
  const post = makePost(config)
  // Load self-check: as soon as DSH loads this plugin, /events gets one record.
  post({ hook_event_name: 'aistatus/plugin-loaded', plugin_version: VERSION })
  log('plugin loaded v' + VERSION)

  const events = [
    'session/created', 'session/close', 'session/disposed', 'session/agent-busy',
    'session/prompt', 'user/message', 'session/event',
    'turn/start', 'turn/end',
    'agent/created', 'agent/request', 'agent/pre-step', 'agent/assistant-stream',
    'agent/error', 'agent/request-error', 'agent/turn-stopping',
    'tools/pre-execute', 'tools/call', 'tool/call', 'tools/execute',
    'tools/post-execute', 'tools/result', 'tool/result',
    'approval/request', 'approval/asked', 'approval/decided',
    'notifications/elicitation', 'question/request', 'ask/user',
    'assistant/message', 'assistant/chunk', 'assistant/live-chunk',
    'assistant/attempt',
  ]

  const lastSent = new Map()

  for (const event of events) {
    try {
      ctx.on(event, (...args) => {
        try {
          const now = Date.now()
          const throttled = THROTTLED.has(event)
          if (!throttled || now - (lastSent.get(event) || 0) >= THROTTLE_MS) {
            lastSent.set(event, now)
            const payload = normalize(event, args)
            if (payload) {
              post(payload)
              log('event ' + event + (payload.hook_event_name !== event
                ? ' -> ' + payload.hook_event_name : ''))
            }
          }
        } catch (e) {
          log('map error ' + event + ': ' + e)
        }
        // Passive observer: always pass waterfall events through.
        const next = args[args.length - 1]
        if (typeof next === 'function') return next()
      })
    } catch (e) {
      log('subscribe failed ' + event + ': ' + e)
    }
  }
  log('subscribed ' + events.length + ' events')
}
