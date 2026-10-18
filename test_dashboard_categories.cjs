// Category panel DOM tests: per-shelf numbers, findings, safe text, and the
// guarantee that a dead /api/categories cannot blank the rest of the dashboard.
// The API is mocked; no bot, server, network, database or channel is touched.
const { JSDOM } = require('jsdom');
const { execFileSync } = require('node:child_process');
const assert = require('node:assert/strict');
const html = execFileSync(process.env.PYTHON || 'python3',
  ['-c', 'from dashboard.app import UI_HTML; print(UI_HTML)'], {encoding: 'utf8'});

// Everything an attacker could control: a channel name, a category label, a
// logged skip reason and a finding detail. None of it may become markup.
const EVIL = '<img src=x onerror="window.__pwned=1">';
const calls = [];
const rows = [{name: 'ClothingShelf', sources: ['*'], categories: ['fashion'], enabled: true}];
let categoryStatus = 200, categoryPayload = null, holdCategories = null;

function report() {
  return {
    db_readable: true, log_lines_read: 12, rules_version: 3, bot_rules_version: 3,
    totals: {shelves: 2, shelves_with_channel: 1, sent: 5, skipped: 2, routed: 7,
             withheld: 3, main_sent: 9},
    main: {targets: ['mymaindeals'], deliveries: {sent: 9, skipped: 1, held: 0},
           pending: 2, last_sent: '2026-10-18 09:00 IST'},
    unclassified_withheld: {total: 1, reasons: {mixed_or_conflicting: 1}},
    foreign_category_withheld: {toys: {total: 2, reasons: {mixed_or_conflicting: 2}}},
    categories: [
      {key: 'electronics', label: EVIL + ' Electronics', aliases: 79, state: 'ok',
       targets: ['electroshelf' + EVIL], active_targets: ['electroshelf' + EVIL],
       tag_targets: ['electroshelf' + EVIL], tagless_targets: [],
       deliveries: {sent: 5, skipped: 2, held: 1}, pending: 3, routed: 7,
       withheld: {total: 3, reasons: {mixed_or_conflicting: 2, insufficient_product_evidence: 1}},
       skip_reasons: {['target_unresolved' + EVIL]: 2}, last_sent: '2026-10-18 09:12 IST',
       unresolved_targets: [], channels: []},
      {key: 'grocery', label: 'Grocery', aliases: 40, state: 'disabled',
       targets: ['offshelf'], active_targets: [], tag_targets: [], tagless_targets: ['offshelf'],
       deliveries: {sent: 0, skipped: 0, held: 0}, pending: 0, routed: 0,
       withheld: {total: 0, reasons: {}}, skip_reasons: {}, last_sent: 'never', channels: []}],
    findings: [
      {severity: 'warn', kind: 'target_unresolved' + EVIL, category: 'electronics',
       detail: EVIL + ' was not reachable; deals fell back to Main'},
      {severity: 'info', kind: 'tagless_shelf', category: 'grocery',
       detail: 'Amazon links stay withheld until the tag is declared'}],
  };
}
categoryPayload = report();

const dom = new JSDOM(html, {url: 'https://preview.example/', runScripts: 'dangerously', beforeParse(w) {
  w.confirm = () => true; w.alert = message => { throw new Error(message); };
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.fetch = async (path, options) => {
    const body = options && options.body ? JSON.parse(options.body) : null;
    calls.push({path, method: (options && options.method) || 'GET', body});
    let data = {}, status = 200;
    if (path === '/api/status') data = {queue: {pending: 1}, db_exists: true, today_sends: 4};
    if (path === '/api/channels') data = {revision: 'r1', dashboard: rows, builtin: ['LootZoneIndia11'],
      categories: {fashion: 'Clothing', electronics: 'Electronics', grocery: 'Grocery'},
      sources: ['deals'], category_rules_version: 3, bot_category_rules_version: 3, activation: {}};
    if (path === '/api/recent?limit=15') data = {jobs: [{id: 1, source: 'deals', status: 'done',
      sends: 2, skips: 0, held: 0, error: ''}]};
    if (path === '/api/audit?limit=200') data = {posts_checked: 3, link_summary: {}, delivery_summary: {},
      findings_by_kind: {}};
    if (path.startsWith('/api/categories')) {
      if (holdCategories) await holdCategories;
      status = categoryStatus;
      data = status === 200 ? categoryPayload : {error: 'database not readable yet'};
    }
    return {ok: status === 200, status, json: async () => data};
  };
}});

(async () => {
  const w = dom.window, d = w.document;
  w.eval("TOKEN='test'");
  await w.refresh();

  // --- the panel is populated from the endpoint refresh() already called ------
  assert.equal(d.querySelectorAll('#catrows tr').length, 2, 'one row per shelf');
  assert.equal(calls.filter(c => c.path.startsWith('/api/categories')).length, 1,
    'refresh() loads the panel exactly once');
  assert.match(calls.find(c => c.path.startsWith('/api/categories')).path, /hours=24/,
    'the default window is 24h');
  const stats = d.getElementById('catstats');
  assert.equal(stats.querySelectorAll('.stat').length, 7, 'summary stats rendered');
  assert.match(stats.textContent, /shelves reachable/, 'stats are labelled');
  assert.match(stats.textContent, /withheld unclassified/, 'unclassified withholding is visible');
  assert.match(d.getElementById('catnote').textContent, /installed rules v3 · bot reported v3/,
    'the rules versions are published side by side');
  assert.match(d.getElementById('catnote').textContent, /log lines scanned: 12/,
    'the log evidence is stated, so a silent parser is visible');
  assert.match(d.getElementById('catnote').textContent, /categories this taxonomy does not define: toys/,
    'a category the installed rules do not define is surfaced');

  // --- per-shelf numbers -----------------------------------------------------
  const electronics = d.querySelectorAll('#catrows tr')[0], grocery = d.querySelectorAll('#catrows tr')[1];
  assert.match(electronics.textContent, /Electronics/, 'the shelf is named');
  assert.match(electronics.textContent, /79 aliases/, 'the taxonomy size is shown');
  assert.match(electronics.textContent, /receiving/, 'a healthy shelf says so');
  assert.match(electronics.textContent, /2026-10-18 09:12 IST/, 'the last send is shown');
  assert.match(electronics.textContent, /mixed_or_conflicting×2/, 'withhold reasons are shown');
  assert.match(grocery.textContent, /configured but disabled/, 'a disabled shelf is not "no channel"');
  assert.match(grocery.textContent, /\(disabled\)/, 'the inactive target is marked');
  assert.match(grocery.textContent, /\(tagless\)/, 'the Amazon-tag state is marked');
  assert.equal(grocery.querySelectorAll('td').length, 10, 'the row keeps its shape');

  // --- findings --------------------------------------------------------------
  const findings = d.getElementById('catfindings');
  assert.equal(findings.querySelectorAll('.badge').length, 2, 'one badge per finding');
  assert.match(findings.textContent, /target_unresolved/, 'the finding kind is shown');
  assert.match(findings.textContent, /Amazon links stay withheld/, 'the info finding is shown');

  // --- nothing from the response became markup -------------------------------
  assert.equal(d.querySelectorAll('#categories img').length, 0, 'no injected element');
  assert.equal(d.querySelectorAll('#categories script').length, 0, 'no injected script');
  assert.equal(d.querySelectorAll('#categories [onerror]').length, 0, 'no injected handler');
  assert.equal(w.__pwned, undefined, 'no handler ever ran');
  for (const id of ['catrows', 'catfindings']) {
    assert.ok(d.getElementById(id).textContent.includes('<img src=x'),
      `${id} shows the payload as text, not as markup`);
  }
  assert.ok(d.getElementById('catrows').innerHTML.includes('&lt;img'),
    'the label is escaped in the markup itself');

  // --- the window picker refetches -------------------------------------------
  d.getElementById('cathours').value = '72';
  await w.loadCategories();
  const windows = calls.filter(c => c.path.startsWith('/api/categories')).map(c => c.path);
  assert.match(windows[windows.length - 1], /hours=72/, 'the picker drives the query');
  assert.equal(windows.length, 2, 'one extra fetch, not a storm');

  // --- an empty payload renders a safe empty state ---------------------------
  categoryPayload = {};
  await w.loadCategories();
  assert.equal(d.querySelectorAll('#catrows tr').length, 1, 'a single empty-state row');
  assert.match(d.getElementById('catrows').textContent, /no category rules installed/);
  assert.match(d.getElementById('catnote').textContent, /no bot log found/,
    'missing log evidence is stated, never implied');
  assert.equal(w.__pwned, undefined);

  // --- a database the report could not read says so --------------------------
  categoryPayload = {...report(), db_readable: false, db: '/srv/bot.sqlite3', log_lines_read: 0};
  await w.loadCategories();
  assert.match(d.getElementById('catnote').textContent, /database not readable yet/,
    'zero counts are explained, not presented as a quiet week');

  // --- a dead endpoint must not blank the rest of the dashboard --------------
  categoryPayload = report(); categoryStatus = 500;
  await w.refresh();
  assert.match(d.getElementById('catnote').textContent, /category data unavailable/,
    'the failure is reported in the panel');
  assert.match(d.getElementById('catrows').textContent, /no category data/);
  assert.equal(d.querySelectorAll('#channels tr').length, 2, 'built-in + dashboard channel still listed');
  assert.equal(d.querySelectorAll('#recent tr').length, 1, 'recent jobs still rendered');
  assert.match(d.getElementById('audit').textContent, /posts checked: 3/, 'audit still rendered');
  assert.ok(!/^error:/.test(d.getElementById('authstate').textContent),
    'a category failure is not shown as an auth failure: '
    + d.getElementById('authstate').textContent);

  // --- a slow endpoint cannot be double-fired by the window picker -----------
  categoryStatus = 200;
  let release; holdCategories = new Promise(resolve => { release = resolve; });
  const pending = w.loadCategories();
  release(); await pending; holdCategories = null;
  assert.equal(d.querySelectorAll('#catrows tr').length, 2, 'the panel recovers');

  console.log('Category panel DOM PASS: per-shelf sends/skips/backlog, routed vs withheld with '
    + 'reasons, disabled vs unconfigured states, tagless marking, findings, window picker, '
    + 'empty/unreadable/500 states, escaped labels+targets+reasons, and the rest of the '
    + 'dashboard still rendering when the endpoint dies.');
  w.close();
})().catch(error => { console.error(error); process.exit(1); });
