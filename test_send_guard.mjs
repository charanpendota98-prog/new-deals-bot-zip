import test from 'node:test'
import assert from 'node:assert/strict'
import { guardedSend, DeliveryHeld } from './tg-wa-bridge/send_guard.js'

function fixture() {
  let disk = {}, calls = 0
  const state = {}
  const opts = { state, save: () => { disk = structuredClone(state) }, target: 'A', owner: 'job1',
    tag: 'text', body: 'Acme X31 ₹999', products: ['ASIN:B0ABC12345'], send: async () => { calls++ } }
  return { opts, calls: () => calls, disk: () => disk }
}
test('lost response never triggers automatic repeat, including after restart', async () => {
  const f = fixture()
  const send = async () => { await f.opts.send(); throw new Error('timeout after acceptance') }
  await assert.rejects(guardedSend({ ...f.opts, send }), DeliveryHeld)
  await assert.rejects(guardedSend({ ...f.opts, state: f.disk(), send }), DeliveryHeld)
  assert.equal(f.calls(), 1)
})
test('write-before-send failure aborts before calling the socket', async () => {
  const f = fixture()
  await assert.rejects(guardedSend({ ...f.opts, save: () => { throw new Error('disk full') } }))
  assert.equal(f.calls(), 0)
})
test('crash after intent has the same durable hold as a timeout', async () => {
  const f = fixture()
  await guardedSend(f.opts)
  const state = f.disk()
  Object.values(state.sendIntents)[0].status = 'started'
  await assert.rejects(guardedSend({ ...f.opts, state }), DeliveryHeld)
  assert.equal(f.calls(), 1)
})
test('failed success receipt remains fenced through process restart', async () => {
  const f = fixture()
  let writes = 0
  const save = () => { if (writes++) throw new Error('receipt disk failure'); f.opts.save() }
  await assert.rejects(guardedSend({ ...f.opts, save }), DeliveryHeld)
  await assert.rejects(guardedSend({ ...f.opts, state: f.disk() }), DeliveryHeld)
  assert.equal(f.calls(), 1)
})
test('confirmed receipt repairs lost outer job mark without sending again', async () => {
  const f = fixture()
  await guardedSend(f.opts)
  await guardedSend({ ...f.opts, state: f.disk() })
  assert.equal(f.calls(), 1)
})
test('changed payload on the same logical chunk is withheld', async () => {
  const f = fixture()
  await guardedSend(f.opts)
  await assert.rejects(guardedSend({ ...f.opts, body: 'different caption' }), DeliveryHeld)
  assert.equal(f.calls(), 1)
})
test('simultaneous dispatch cannot pass an active product fence', async () => {
  const f = fixture()
  let release, entered
  const started = new Promise(resolve => { entered = resolve })
  const wait = new Promise(resolve => { release = resolve })
  const first = guardedSend({ ...f.opts, send: async () => { await f.opts.send(); entered(); await wait } })
  await started
  try {
    await assert.rejects(guardedSend({ ...f.opts, owner: 'other-source', body: 'reworded product' }), DeliveryHeld)
  } finally { release() }
  await first
  assert.equal(f.calls(), 1)
})
test('held photo forbids same-job text fallback and cross-source copy, but not another target', async () => {
  const f = fixture()
  await assert.rejects(guardedSend({ ...f.opts, tag: 'photo', send: async () => { await f.opts.send(); throw Error('lost') } }), DeliveryHeld)
  await assert.rejects(guardedSend({ ...f.opts, tag: 'fallback' }), DeliveryHeld)
  await assert.rejects(guardedSend({ ...f.opts, owner: 'other', body: 'sale' }), DeliveryHeld)
  await guardedSend({ ...f.opts, target: 'B' })
  assert.equal(f.calls(), 2)
})
test('absolute one-hour fence ignores cheaper caption; a later re-offer is allowed', async () => {
  const f = fixture()
  await guardedSend({ ...f.opts, now: () => 100000 })
  await assert.rejects(guardedSend({ ...f.opts, owner: 'cheaper', body: '₹1', now: () => 3699999 }), DeliveryHeld)
  await guardedSend({ ...f.opts, owner: 'later', body: '₹499', now: () => 3700001 })
  assert.equal(f.calls(), 2)
})
test('distinct products and subsequent chunks of same job still work', async () => {
  const f = fixture()
  await guardedSend(f.opts)
  await guardedSend({ ...f.opts, tag: 'tail', body: 'more details' })
  await guardedSend({ ...f.opts, owner: 'other', products: ['ASIN:B0OTHER123'], body: 'other product' })
  assert.equal(f.calls(), 3)
})

test('cleanup bounds confirmed history, never unresolved or active-job fences', async () => {
  const { pruneConfirmedFences } = await import('./tg-wa-bridge/send_guard.js')
  const now = 10 * 24 * 3600_000
  const records = {
    old: { status: 'sent', at: 1, owner: 'done' },
    active: { status: 'sent', at: 1, owner: 'active' },
    recent: { status: 'sent', at: now, owner: 'done' },
    held: { status: 'held', at: 1, owner: 'done' },
    started: { status: 'started', at: 1, owner: 'done' },
  }
  const state = { jobs: [{ id: 'active' }], sendIntents: structuredClone(records), targetProductFences: structuredClone(records) }
  pruneConfirmedFences(state, now)
  for (const field of ['sendIntents', 'targetProductFences']) {
    assert.deepEqual(Object.keys(state[field]).sort(), ['active', 'held', 'recent', 'started'])
  }
})

test('real bridge snapshot writer preserves intents in both recovery files and fails closed on corrupt history', async () => {
  const fs = await import('node:fs')
  const os = await import('node:os')
  const path = await import('node:path')
  const vm = await import('node:vm')
  const source = fs.readFileSync(new URL('./tg-wa-bridge/bridge.js', import.meta.url), 'utf8')
  const code = source.slice(source.indexOf('function loadState() {'), source.indexOf('function sleep('))
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-fence-snapshots-'))
  try {
    const context = { fs, SELF_TEST: false, STATE_FILE: path.join(dir, 'state.json'),
      STATE_BACKUP_FILE: path.join(dir, 'backup.json'), defaultState: () => ({ jobs: [] }),
      log: { warn() {} }, state: { sendIntents: { first: { status: 'started' } } } }
    vm.createContext(context)
    vm.runInContext(code, context)
    assert.equal(JSON.stringify(context.loadState()), '{"jobs":[]}')
    context.saveState()
    context.state.sendIntents.second = { status: 'started' }
    context.saveState()
    for (const file of [context.STATE_FILE, context.STATE_BACKUP_FILE]) {
      assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), context.state)
    }
    fs.writeFileSync(context.STATE_FILE, '{corrupt')
    assert.deepEqual(JSON.parse(JSON.stringify(context.loadState())), { jobs: [], ...context.state })
    fs.writeFileSync(context.STATE_BACKUP_FILE, '{corrupt')
    assert.throws(() => context.loadState(), /refusing to forget send fences/)
  } finally { fs.rmSync(dir, { recursive: true, force: true }) }
})
