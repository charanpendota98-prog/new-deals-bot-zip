// Write-before-send, at-most-once safety for uncertain remote outcomes.
// This intentionally prefers an operator hold over publishing a second copy.
import { createHash } from 'node:crypto'
export class DeliveryHeld extends Error {
  constructor(message) { super(`manual review: ${message}`); this.name = 'DeliveryHeld' }
}
const hash = value => createHash('sha256').update(value).digest('hex')
export async function guardedSend({ state, save, target, owner, tag, body, products = [], send, now = Date.now }) {
  state.sendIntents ||= {}
  state.targetProductFences ||= {}
  const key = hash(JSON.stringify([target, owner, tag]))
  const payloadHash = hash(body || '')
  const fenceKeys = [...new Set(products)].map(product => hash(JSON.stringify([target, product])))
  const previous = state.sendIntents[key]
  if (previous && previous.payloadHash !== payloadHash) throw new DeliveryHeld(`changed payload for ${target}`)
  if (previous?.status === 'sent') return // receipt persisted; never resend on a lost job mark
  if (previous) throw new DeliveryHeld(`unsettled send to ${target}`)
  for (const fenceKey of fenceKeys) {
    const fence = state.targetProductFences[fenceKey]
    if (fence && (fence.status !== 'sent' || (fence.owner !== owner && now() - fence.at < 3600_000))) {
      throw new DeliveryHeld(`product has a recent or unsettled send to ${target}`)
    }
  }
  const intent = { target, owner, tag, payloadHash, status: 'started', at: now() }
  state.sendIntents[key] = intent
  for (const fenceKey of fenceKeys) state.targetProductFences[fenceKey] = { owner, status: 'started', at: now() }
  // A failed pre-send save aborts BEFORE any remote publication. Keep the
  // in-memory fence too: never let a second dispatch race the first one.
  save()
  try {
    await send()
    intent.status = 'sent'
    intent.at = now()
    for (const fenceKey of fenceKeys) state.targetProductFences[fenceKey] = { owner, status: 'sent', at: now() }
    save()
  } catch (error) {
    intent.status = 'held'
    intent.error = String(error?.message || error).slice(0, 300)
    for (const fenceKey of fenceKeys) state.targetProductFences[fenceKey] = { owner, status: 'held', at: now() }
    try { save() } catch {} // the already-durable 'started' fence still protects restarts
    throw new DeliveryHeld(`send to ${target} may have succeeded; automatic retry/fallback disabled`)
  }
}

// Retain unresolved outcomes indefinitely. Bound only confirmed history for
// jobs no longer queued; never discard an active job's restart protection.
export function pruneConfirmedFences(state, now = Date.now()) {
  const active = new Set((state.jobs || []).map(job => String(job.id)))
  const cutoff = now - 7 * 24 * 3600_000
  for (const field of ['sendIntents', 'targetProductFences']) {
    for (const [key, item] of Object.entries(state[field] || {})) {
      if (item.status === 'sent' && item.at < cutoff && !active.has(item.owner)) delete state[field][key]
    }
  }
}
