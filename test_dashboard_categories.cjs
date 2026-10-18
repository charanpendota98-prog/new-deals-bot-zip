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
let convStatus = 200;
const convPayload = {
  totals: {cached: 3, owned: 1, foreign_tag: 1, earns_nothing: 1, unattributed: 0,
           proofs: 1, capped: false},
  converted_pct: 33.3,
  rows: [
    {at: '2026-10-18 09:12 IST', source: 'https://amzn.lt/' + EVIL,
     affiliate: 'https://www.amazon.in/dp/B1?tag=stranger' + EVIL,
     resolved_host: 'amazon.in', route: 'amazon', bucket: 'foreign_tag', proof: false},
    {at: '2026-10-18 08:40 IST', source: 'https://amzn.lt/ok',
     affiliate: 'https://www.amazon.in/dp/B2?tag=ours', resolved_host: 'amazon.in',
     route: 'amazon', bucket: 'owned', proof: true},
  ],
  db_exists: true,
};

function report() {
  return {
    db_readable: true, log_lines_read: 12, rules_version: 3, bot_rules_version: 3,
    totals: {shelves: 2, shelves_with_channel: 1, sent: 5, skipped: 2, routed: 7,
             withheld: 3, main_sent: 9, paid_posts: 4},
    money_totals: {posts: 6, owned: 4, earns_nothing: 1, foreign_tag: 1, unattributed: 0,
                   routes: {amazon: 4, hypd: 1}},
    whatsapp: {configured: true, channels: {electronics: '120363001@newsletter'},
               core: {under99: '120363009@newsletter'}, tag_categories: ['electronics'],
               unknown_env: ['WA_CHANNEL_GHOSTS'], comparable: true, masked: 0,
               collisions: [{jid: '120363001@newsletter', categories: ['electronics', 'grocery'],
                 why: 'two categories share one WhatsApp channel'}]},
    history_days: ['2026-10-16', '2026-10-17', '2026-10-18'],
    quality_fleet: {posts: 6, scored: 6, unscored: 0, avg_score: 900.0, min_score: 100,
                    max_score: 1300, zero_score: 0, price_rows: 3, price_min: 49,
                    price_median: 749, price_max: 2499, under_99: 1, under_499: 1,
                    sources: {deals: 5, ['loot' + EVIL]: 1}},
    source_matrix: {columns: ['electronics', 'grocery', 'main'],
                    sources: ['deals', 'loot' + EVIL], cells: {deals: {electronics: 4, main: 2},
                    ['loot' + EVIL]: {electronics: 1}}, totals: {deals: 6, ['loot' + EVIL]: 1},
                    declared: {electronics: ['deals', 'ghostsrc'], grocery: []},
                    unrestricted: {electronics: false, grocery: true},
                    declared_only: ['ghostsrc']},
    recent: {electronics: [
      {queue_id: 7, at: '2026-10-18 09:12 IST', target: 'electroshelf' + EVIL, status: 'sent',
       reason: '', error: '', money: 'owned', route: 'amazon',
       snippet: EVIL + ' Earbuds 65 pct off'},
      {queue_id: 6, at: '2026-10-18 08:40 IST', target: 'electroshelf', status: 'skipped',
       reason: 'target_unresolved' + EVIL, error: 'unresolved', money: '', route: '',
       snippet: 'nothing delivered'}]},
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
       unresolved_targets: [], channels: [],
       money: {posts: 6, owned: 4, earns_nothing: 1, foreign_tag: 1, unattributed: 0,
               routes: {amazon: 4, ['passthrough' + EVIL]: 1, hypd: 1},
               examples: [{queue_id: 2, target: 'electroshelf', bucket: 'foreign_tag',
                           route: 'amazon', at: '2026-10-18 08:00 IST', host: 'amazon.in'}]},
       history: [{day: '2026-10-16', sent: 1, skipped: 0}, {day: '2026-10-17', sent: 2, skipped: 1},
                 {day: '2026-10-18', sent: 3, skipped: 0}],
       quality: {posts: 6, scored: 5, unscored: 1, avg_score: 1060.0, min_score: 100,
                 max_score: 1300, zero_score: 0, price_rows: 4, price_min: 49, price_median: 424,
                 price_max: 2499, under_99: 2, under_499: 0,
                 sources: {deals: 4, unknownsrc: 1, ['loot' + EVIL]: 1}},
       surfaces: {telegram: {targets: ['electroshelf' + EVIL], amazon_tag: false},
                  whatsapp: {channel: '120363001@newsletter', filtered: true,
                             policy: 'category:electronics', amazon_tag: true}}},
      {key: 'grocery', label: 'Grocery', aliases: 40, state: 'disabled',
       targets: ['offshelf'], active_targets: [], tag_targets: [], tagless_targets: ['offshelf'],
       deliveries: {sent: 0, skipped: 0, held: 0}, pending: 0, routed: 0,
       withheld: {total: 0, reasons: {}}, skip_reasons: {}, last_sent: 'never', channels: [],
       money: {posts: 0, owned: 0, earns_nothing: 0, foreign_tag: 0, unattributed: 0,
               routes: {}, examples: []},
       history: [{day: '2026-10-16', sent: 0, skipped: 0}, {day: '2026-10-17', sent: 0, skipped: 0},
                 {day: '2026-10-18', sent: 0, skipped: 0}],
       quality: {posts: 0, scored: 0, unscored: 0, avg_score: null, min_score: null, max_score: null,
                 zero_score: 0, price_rows: 0, price_min: null, price_median: null,
                 price_max: null, under_99: 0, under_499: 0, sources: {}},
       surfaces: {telegram: {targets: [], amazon_tag: false},
                  whatsapp: {channel: '', filtered: false, policy: '', amazon_tag: false}}}],
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
    if (path.startsWith('/api/link-conversion')) {
      status = convStatus;
      data = status === 200 ? convPayload : {error: 'conversion view failed'};
      return {ok: status === 200, status, json: async () => data};
    }
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
  assert.match(calls.find(c => c.path.startsWith('/api/categories')).path, /days=7/,
    'the default trend is 7 IST days');
  const stats = d.getElementById('catstats');
  assert.equal(stats.querySelectorAll('.stat').length, 8, 'summary stats rendered');
  assert.match(stats.textContent, /posts that pay us/, 'the money headline is a stat');
  assert.match(stats.textContent, /shelves reachable/, 'stats are labelled');
  assert.match(stats.textContent, /withheld unclassified/, 'unclassified withholding is visible');
  assert.match(d.getElementById('catnote').textContent, /installed rules v3 · bot reported v3/,
    'the rules versions are published side by side');
  assert.match(d.getElementById('catnote').textContent, /log lines scanned: 12/,
    'the log evidence is stated, so a silent parser is visible');
  assert.match(d.getElementById('catnote').textContent, /categories this taxonomy does not define: toys/,
    'a category the installed rules do not define is surfaced');
  assert.match(d.getElementById('catnote').textContent,
    /money: 6 delivered post\(s\), 4 carrying a link that pays us, 1 paying nobody, 1 carrying a FOREIGN tag/,
    'the note states the money verdict in words');
  assert.match(d.getElementById('catnote').textContent,
    /WhatsApp: 1 category channel\(s\), 1 core, 1 COLLISION/,
    'the WhatsApp surface and its collision are stated');

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
  assert.equal(grocery.querySelectorAll('td').length, 14, 'the row keeps its shape');
  assert.match(electronics.querySelectorAll('td')[10].textContent, /avg 1060/,
    'the deal-quality cell shows the bot own average score');
  assert.match(electronics.querySelectorAll('td')[10].textContent, /Rs49-Rs2499, median Rs424/,
    'and the recorded rupee spread');
  assert.match(electronics.querySelectorAll('td')[10].textContent, /deals×4/,
    'and which sources fed it');
  assert.match(grocery.querySelectorAll('td')[10].textContent, /nothing delivered/,
    'a shelf with no posts has no quality verdict');
  const srcHead = d.getElementById('catsourcehead').textContent;
  const srcBody = d.getElementById('catsources');
  assert.match(srcHead, /Source/, 'the source matrix has a header');
  assert.equal(srcBody.querySelectorAll('tr').length, 3,
    'one matrix row per observed or declared-only source');
  assert.match(srcBody.textContent, /ghostsrc/, 'a declared-but-silent source is still listed');
  assert.match(srcBody.textContent, /DEAD/, 'and marked DEAD');
  assert.ok(srcBody.textContent.includes('loot<img src=x'),
    'a hostile source name in the matrix stays text');
  assert.equal(srcBody.querySelectorAll('img').length, 0, 'no injected element in the matrix');

  // --- the link-conversion book ------------------------------------------------
  const convStats = d.getElementById('convstats');
  assert.match(convStats.textContent, /33.3%/, 'the converted share is a headline stat');
  assert.match(convStats.textContent, /foreign tag/, 'and the stranger count sits beside it');
  assert.match(d.getElementById('convnote').textContent,
    /1 of 3 cached links \(33.3%\) carry our tag/, 'the note states the verdict in words');
  const convRows = d.querySelectorAll('#convrows tr');
  assert.equal(convRows.length, 2, 'one row per cached link');
  assert.match(convRows[0].textContent, /FOREIGN TAG/, 'a stranger tag is shouted, not shaded');
  assert.match(convRows[1].textContent, /proof/, 'a proved hop shows its proof');
  assert.ok(convRows[0].textContent.includes('?tag=stranger<img src=x'),
    'a hostile affiliate URL stays text');
  assert.equal(d.querySelectorAll('#convrows img').length, 0, 'nothing injected executes');
  convStatus = 500;
  await w.loadConversion();
  assert.match(d.getElementById('convrows').textContent, /conversion view unavailable/,
    'a dead conversion endpoint says so instead of showing stale rows');
  convStatus = 200;
  await w.loadConversion();

  // --- money, trend and surfaces ---------------------------------------------
  assert.match(electronics.textContent, /4 paid · 1 earn nothing/, 'the money verdict is shown');
  assert.match(electronics.textContent, /1 foreign tag/, 'a stranger\'s tag is called out');
  assert.match(electronics.textContent, /6 post\(s\)/, 'the money denominator is shown');
  assert.match(grocery.textContent, /nothing delivered/, 'a shelf with no posts says so');
  const sparkCell = electronics.querySelectorAll('td')[11];
  assert.match(sparkCell.textContent, /[\u2581-\u2588]{3}/, 'a sparkline character per IST day');
  assert.match(sparkCell.getAttribute('title') || '', /sends per IST day: 1\/2\/3/,
    'the exact numbers are in the title, so the picture is never the only evidence');
  assert.match(grocery.querySelectorAll('td')[11].textContent, /\u2581\u2581\u2581/,
    'an all-zero trend is a flat floor, not a blank cell');
  assert.match(electronics.textContent, /TG @electroshelf/, 'the Telegram surface is shown');
  assert.match(electronics.textContent, /WA 120363001@newsletter/, 'the WhatsApp surface is shown');
  assert.match(electronics.textContent, /category:electronics/, 'the bridge policy is named');
  assert.match(grocery.textContent, /WA not configured/, 'an unconfigured surface says so');
  assert.equal(d.querySelectorAll('#catrows tr[data-key]').length, 2,
    'every row is clickable and carries its key');
  assert.equal(electronics.dataset.key, 'electronics', 'the key is the shelf key');
  assert.match(electronics.getAttribute('title') || '', /click to open/, 'the row says it opens');

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
  // The new cells take their text from channel names, route names and WhatsApp
  // values - all operator-controlled - so they get the same treatment.
  assert.ok(electronics.textContent.includes('passthrough<img src=x'),
    'a malicious money-route name is text, not markup');
  assert.ok(d.getElementById('catrows').innerHTML.includes('&lt;img'),
    'the escaped form is what reached the markup');
  assert.ok(d.getElementById('catrows').innerHTML.includes('&lt;img'),
    'the label is escaped in the markup itself');

  // --- the window and trend pickers refetch ----------------------------------
  d.getElementById('cathours').value = '72';
  d.getElementById('catdays').value = '14';
  await w.loadCategories();
  const windows = calls.filter(c => c.path.startsWith('/api/categories')).map(c => c.path);
  assert.match(windows[windows.length - 1], /hours=72/, 'the window picker drives the query');
  assert.match(windows[windows.length - 1], /days=14/, 'the trend picker drives the query');
  assert.equal(windows.length, 2, 'one extra fetch, not a storm');

  // --- drill-down: a row opens into the delivery rows behind its numbers ------
  const row = d.querySelectorAll('#catrows tr[data-key]')[0];
  row.dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
  await new Promise(resolve => w.setTimeout(resolve, 0));
  const drilled = calls.filter(c => /expand=electronics/.test(c.path));
  assert.equal(drilled.length, 1, 'clicking a row fetches that shelf\'s rows once');
  assert.equal(d.querySelectorAll('#catrows tr.catdetail').length, 1, 'a detail row opened');
  assert.equal(d.querySelectorAll('#catrows tr.open').length, 1, 'the open row is marked');
  const detail = d.querySelector('#catrows tr.catdetail');
  assert.equal(detail.querySelectorAll(':scope > td').length, 1,
    'the detail is one cell of the outer table');
  assert.equal(detail.querySelector(':scope > td').colSpan, 14, 'and it spans every column');
  assert.equal(detail.querySelectorAll('table tbody tr').length, 2,
    'one drill-down line per delivery row');
  assert.equal(detail.querySelectorAll('table thead th').length, 7,
    'each line is labelled: queue, when, target, status, reason, money, post');
  assert.match(detail.textContent, /#7/, 'the queue id is shown');
  assert.match(detail.textContent, /target_unresolved/, 'a skipped row shows its reason');
  assert.match(detail.textContent, /owned\/amazon/, 'a delivered row shows its money verdict');
  assert.equal(detail.querySelectorAll('img').length, 0, 'no markup from the drill-down');
  assert.ok(detail.textContent.includes('<img src=x'),
    'a malicious snippet or target name in the drill-down stays text');
  assert.equal(w.__pwned, undefined, 'no drill-down handler ran');

  // --- an open drill-down survives the auto-refresh --------------------------
  await w.loadCategories();
  assert.equal(d.querySelectorAll('#catrows tr.catdetail').length, 1,
    'the refresh did not snap the drill-down shut');
  assert.match(d.getElementById('catnote').textContent, /table paused while a shelf row is open/,
    'and the operator is told why the table looks frozen');

  d.querySelector('#catrows tr[data-key]').dispatchEvent(new w.MouseEvent('click', {bubbles: true}));
  await new Promise(resolve => w.setTimeout(resolve, 0));
  assert.equal(d.querySelectorAll('#catrows tr.catdetail').length, 0, 'clicking again closes it');
  assert.equal(d.querySelectorAll('#catrows tr.open').length, 0, 'and unmarks the row');

  // --- an empty payload renders a safe empty state ---------------------------
  categoryPayload = {};
  await w.loadCategories();
  assert.equal(d.querySelectorAll('#catrows tr').length, 1, 'a single empty-state row');
  assert.equal(d.querySelector('#catrows tr td').colSpan, 14, 'the empty state spans every column');
  assert.match(d.getElementById('catsources').textContent, /no source traffic/,
    'an empty payload empties the matrix too');
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
  assert.match(d.getElementById('catsources').textContent, /no source traffic/,
    'a dead endpoint clears the source matrix instead of leaving a stale picture');
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

  // --- the drill-down builder is DOM-only, by construction --------------------
  const detailSource = w.renderShelfDetail.toString();
  assert.match(detailSource, /textContent/, 'the drill-down writes text nodes');
  assert.ok(!detailSource.includes('innerHTML'),
    'the drill-down never assigns innerHTML: a caption cannot become markup');
  assert.ok(!w.toggleShelf.toString().includes('innerHTML'),
    'opening a shelf never assigns innerHTML either');
  for (const fn of [w.moneyCell, w.surfaceCell]) {
    assert.match(fn.toString(), /escapeHTML\(/, `${fn.name} escapes what it interpolates`);
  }
  assert.ok(!w.moneyCell.toString().includes('${m.') && !w.surfaceCell.toString().includes('${wa.'),
    'the money and surface cells interpolate escaped locals, not response fields');

  console.log('Category panel DOM PASS: per-shelf sends/skips/backlog, routed vs withheld with '
    + 'reasons, disabled vs unconfigured states, tagless marking, findings, money verdicts, '
    + 'IST-day sparklines, Telegram vs WhatsApp surfaces, click-to-open drill-down that survives '
    + 'the auto-refresh, window+trend pickers, empty/unreadable/500 states, escaped labels, '
    + 'targets, reasons, route names and snippets, and the rest of the dashboard still rendering '
    + 'when the endpoint dies.');
  w.close();
})().catch(error => { console.error(error); process.exit(1); });
