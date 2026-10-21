// Ops console DOM tests: health, live timeline, search, post trace, scorecard.
//
// These four panels are what an operator opens when something is wrong at 2am,
// so the test plays that role: the API is mocked with realistic payloads, and
// every panel must render the reason, keep hostile source text as TEXT, and
// survive a dead endpoint without blanking the rest of the page.
const { JSDOM } = require('jsdom');
const { execFileSync } = require('node:child_process');
const assert = require('node:assert/strict');

const html = execFileSync(process.env.PYTHON || 'python3',
  ['-c', 'from dashboard.app import UI_HTML; print(UI_HTML)'], {encoding: 'utf8'});

const EVIL = '<img src=x onerror="window.__pwned=1">';
const calls = [];
let healthStatus = 200, pulseStatus = 200, traceStatus = 200,
  searchStatus = 200, scoreStatus = 200, briefStatus = 200, trendsStatus = 200;

const healthPayload = {
  overall: 'warn',
  headline: 'Failed posts — 1 failed · FloodWait 30s',
  counts: {ok: 9, warn: 2, fail: 0, unknown: 2},
  checks: [
    {key: 'db_present', title: 'Bot database', state: 'ok', detail: '156 KB · WAL 0 KB', fix: ''},
    {key: 'queue_failures', title: 'Failed posts', state: 'warn',
     detail: '1 failed · FloodWait 30s ' + EVIL,
     fix: 'Open the trace for one of them: the dashboard shows the exact error per target.'},
    {key: 'policy_parity', title: 'Safety policy (bot vs bridge)', state: 'unknown',
     detail: 'no deployed bridge on this host', fix: 'Set BRIDGE_DIR=~/tg-wa-bridge'},
  ],
  facts: {db_size: 159744, bot_uptime_s: 7200},
};

const pulsePayload = {
  window_hours: 6, since_ist: '2026-10-20 14:00 IST',
  counts: {intake: 3, send: 2, block: 1, link: 2, proof: 1},
  last: {intake: {at_ist: '2026-10-20 19:47 IST', age: '3m', label: 'intake · done'},
         send: {at_ist: '2026-10-20 19:45 IST', age: '5m', label: 'send · sent'},
         block: null, link: null, proof: null},
  silences: [{kind: 'send', age: '5m', why: 'nothing of this kind in 5m'}],
  capped: false,
  hourly: [
    {at: 1, hour_ist: '17:00', intake: 0, send: 0, block: 0, link: 0, proof: 0},
    {at: 2, hour_ist: '18:00', intake: 1, send: 0, block: 0, link: 0, proof: 0},
    {at: 3, hour_ist: '19:00', intake: 2, send: 2, block: 1, link: 2, proof: 1},
  ],
  events: [
    {at: 1792506000, at_ist: '2026-10-20 19:52 IST', age: '1m', kind: 'block', queue_id: 3,
     source: 'Offerzone_deals', target: 'LootZoneIndia11',
     label: 'refused · presend · gambling_betting',
     detail: 'Gambling / betting — matched “1xbet.com” ' + EVIL, status: '1xbet.com',
     severity: 'warn'},
    {at: 1792505900, at_ist: '2026-10-20 19:50 IST', age: '3m', kind: 'link', queue_id: null,
     source: '', target: '', label: 'link converted',
     detail: 'https://link.amazon/B0gWibFuA → https://bitli.in/neoQLED55', status: 'owned',
     severity: 'ok'},
    {at: 1792505800, at_ist: '2026-10-20 19:47 IST', age: '6m', kind: 'intake', queue_id: 1,
     source: 'DealSauce', target: '', label: 'intake · done',
     detail: 'Samsung neo qled 55” @ ₹60k', status: 'done', severity: 'info'},
  ],
};

const briefPayload = {
  healthy: false,
  headline: '2 things need you: Bot is classifying with v4, this checkout has v5; Failed posts.',
  counts: {error: 1, warn: 1, info: 0},
  items: [
    {id: 'rules_drift:0', kind: 'rules_drift', severity: 'error',
     title: 'Bot is classifying with v4, this checkout has v5',
     detail: 'Category decisions since the last deploy may not match the rules the dashboard shows.',
     fix: 'sudo systemctl restart bestgaa', anchor: '#categories', at: 1792506000},
    {id: 'queue_failures:1', kind: 'queue_failures', severity: 'warn',
     title: 'Failed posts', detail: '1 failed · FloodWait 30s ' + EVIL,
     fix: 'Open the trace for one of them: the dashboard shows the exact error per target.',
     anchor: '#console', at: 1792506000},
  ],
  health: healthPayload, movement: '257 sent, +8.5% vs the 1h before · 166 skipped.',
  rules: {installed: 5, running: 4, max_posts: 30},
  drift: {installed: 5, running: 4, pending_restart: false},
  safety: {last_hour: 1, avg_hour: 2.0, week: 300},
  db_exists: true,
};

const trendsPayload = {
  window_label: '24h', window_hours: 24, since_ist: '2026-10-19 20:00 IST',
  previous_since_ist: '2026-10-18 20:00 IST', buckets: 24, backlog: 12,
  verdict: '6,800 sent, -0.9% vs the 24h before · 4,178 skipped · 240 refused by the content policy.',
  metrics: [
    {key: 'sent', label: 'Sent', current: 6800, previous: 6859, delta: -59, pct: -0.9,
     direction: 'down', sentiment: 'bad', series: [1, 2, 3, 4], previous_series: [2, 2, 2, 2]},
    {key: 'skipped', label: 'Skipped', current: 4178, previous: 4080, delta: 98, pct: 2.4,
     direction: 'up', sentiment: 'bad', series: [1, 1, 1, 1], previous_series: [1, 1, 1, 1]},
    {key: 'refused', label: 'Refused', current: 240, previous: 255, delta: -15, pct: -5.9,
     direction: 'down', sentiment: 'good', series: [1, 2], previous_series: [2, 2]},
    {key: 'converted', label: 'Links converted', current: 1523, previous: 1481, delta: 42, pct: 2.8,
     direction: 'up', sentiment: 'good', series: [1, 2], previous_series: [1, 1]},
    {key: 'intake', label: 'Intake', current: 4992, previous: 5000, delta: -8, pct: -0.2,
     direction: 'down', sentiment: 'bad', series: [1], previous_series: [1]},
    {key: 'failed', label: 'Failed', current: 123, previous: 129, delta: -6, pct: -4.7,
     direction: 'down', sentiment: 'good', series: [0], previous_series: [0]},
    {key: 'backlog', label: 'Queued now', current: 12, previous: null, delta: null, pct: null,
     direction: 'flat', sentiment: 'flat', series: [], previous_series: []},
  ],
  by_metric: {},
  channels: [
    {target: 'LootZoneIndia11' + EVIL, sent: 1713, previous_sent: 1727, delta: -14, pct: -0.8,
     skipped: 1046, previous_skipped: 999, sentiment: 'bad'},
    {target: 'PowerLoots1', sent: 1721, previous_sent: 1706, delta: 15, pct: 0.9,
     skipped: 1107, previous_skipped: 1001, sentiment: 'good'},
  ],
  skip_reasons: [
    {reason: 'budget: 30 posts/day reached', current: 894, previous: 816, delta: 78, pct: 9.6},
    {reason: 'duplicate deal_key', current: 866, previous: 840, delta: 26, pct: 3.1},
  ],
};
trendsPayload.by_metric = Object.fromEntries(trendsPayload.metrics.map(m => [m.key, m]));

const searchPayload = {
  query: 'qled', count: 2, kinds: {post: 1, refusal: 1}, capped: false,
  results: [
    {kind: 'post', queue_id: 1, at: 1792505800, at_ist: '2026-10-20 19:47 IST',
     source: 'DealSauce', target: '', status: 'done',
     snippet: 'Samsung neo qled 55” @ ₹60k ' + EVIL},
    {kind: 'refusal', queue_id: 3, at: 1792505700, at_ist: '2026-10-20 19:45 IST',
     source: 'Offerzone_deals', target: 'LootZoneIndia11', status: 'presend',
     snippet: 'gambling_betting: 1xbet.com — not a deal'},
  ],
};

const tracePayload = {
  post: {queue_id: 1, chat_id: -1, msg_id: 1, source: 'DealSauce', status: 'done',
         attempts: 1, created_at: 1792505800, created_ist: '2026-10-20 19:47 IST',
         age: '6m', error: '', premium_score: 0,
         text: 'Samsung neo qled 55” @ ₹60k\nhttps://link.amazon/B0gWibFuA'},
  predicted: {
    facts: {price: 60000, mrp: null, discount: null, quality_score: 0,
            premium_eligible: false, title: 'Samsung neo qled'},
    category: {key: 'electronics', label: 'Electronics', reason: 'matched'},
    safety: {allowed: true, severity: 'clean', category: '', label: '', reason: ''},
    links: [], plan: {targets: [{name: 'LootZoneIndia11', phase: 'source route',
                                 why: 'this source is routed here'}], excluded: []},
  },
  planned_vs_actual: [
    {target: 'LootZoneIndia11', phase: 'source route', why: 'this source is routed here',
     verdict: 'delivered', detail: 'posted', chunks: 1},
    {target: 'ElectroShelf', phase: 'shelf', why: 'an electronics shelf',
     verdict: 'skipped', detail: 'content policy: refusal ' + EVIL, chunks: 0},
    {target: 'PowerLoots1', phase: 'source route', why: 'this source is routed here',
     verdict: 'not_attempted', detail: 'planned, but the bot never wrote a delivery row',
     chunks: 0},
  ],
  deliveries: [], attempts: [{target: 'LootZoneIndia11', chunk: 0, state: 'sent',
                               at: 1792505810, at_ist: '2026-10-20 19:47 IST'}],
  blocks: [{stage: 'presend', target: 'ElectroShelf', category: 'gambling_betting',
            label: 'Gambling / betting', matched: '1xbet.com',
            reason: 'Betting spam is not a deal ' + EVIL, policy_version: '2026-10-19.4',
            excerpt: '******** bonus', at: 1792505820, at_ist: '2026-10-20 19:47 IST'}],
  links: [{source: 'https://link.amazon/B0gWibFuA', affiliate: 'https://bitli.in/neoQLED55',
           resolved: 'https://www.amazon.in/dp/B0CZXQ9K7T', deal_key: 'ASIN:B0CZXQ9K7T',
           at: 1792505790, at_ist: '2026-10-20 19:46 IST'}],
  received_by: ['LootZoneIndia11'], withheld_by: ['ElectroShelf', 'PowerLoots1'],
};

const scorePayload = {
  days: 7, since_ist: '2026-10-13 00:00 IST',
  totals: {channels: 2, sends: 7, skips: 3, sources: 2},
  channels: [
    {target: 'LootZoneIndia11', sends: 4, skips: 1, today: 2, total: 5,
     last_send_at: 1792505800, last_send_ist: '2026-10-20 19:47 IST',
     last_send_age_s: 300, last_any_at: 1792505800, last_any_ist: '2026-10-20 19:47 IST',
     top_skip_reason: 'content policy: refusal ' + EVIL, avg_quality: 450.5,
     best_hour_ist: 19, deliver_rate: 80.0},
    {target: 'ElectroShelf', sends: 0, skips: 3, today: 0, total: 3,
     last_send_at: 0, last_send_ist: '—', last_send_age_s: null, last_any_at: 0,
     last_any_ist: '—', top_skip_reason: '', avg_quality: null, best_hour_ist: null,
     deliver_rate: 0.0},
  ],
  sources: [
    {source: 'DealSauce', intakes: 3, done: 2, failed: 1, last_intake_at: 1792505800,
     last_intake_ist: '2026-10-20 19:47 IST', last_intake_age_s: 300, post_rate: 66.7},
    {source: 'TrickXpert' + EVIL, intakes: 1, done: 0, failed: 0,
     last_intake_at: 1792505700, last_intake_ist: '2026-10-20 19:45 IST',
     last_intake_age_s: 600, post_rate: 0.0},
  ],
};

const dom = new JSDOM(html, {url: 'https://preview.example/', runScripts: 'dangerously',
  beforeParse(w) {
    w.confirm = () => true;
    w.alert = message => { throw new Error(message); };
    w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
    w.HTMLDialogElement.prototype.close = function () { this.open = false; };
    w.fetch = async (path, options) => {
      const body = options && options.body ? JSON.parse(options.body) : null;
      calls.push({path, method: (options && options.method) || 'GET', body});
      let data = {}, status = 200;
      if (path === '/api/status') data = {queue: {pending: 1}, db_exists: true, today_sends: 4};
      if (path === '/api/channels') data = {revision: 'r1', dashboard: [], builtin: [],
        categories: {electronics: 'Electronics'}, sources: ['DealSauce'],
        category_rules_version: 4, bot_category_rules_version: 4, activation: {}};
      if (path === '/api/recent?limit=15' || path.startsWith('/api/recent')) {
        data = {jobs: [{id: 1, source: 'DealSauce', status: 'done', sends: 1, skips: 0,
                        held: 0, error: ''}]};
      }
      if (path === '/api/audit?limit=200') data = {posts_checked: 1, link_summary: {},
        delivery_summary: {}, findings_by_kind: {}};
      if (path.startsWith('/api/link-conversion')) data = {totals: {}, rows: [], trend: []};
      if (path.startsWith('/api/post-buttons')) data = {buttons: [], max_buttons: 2};
      if (path.startsWith('/api/categories')) { status = 500; data = {error: 'dead'}; }
      if (path.startsWith('/api/safety')) data = {policy: {version: '2026-10-19.4',
        categories: []}, totals: {today: 0, window: 0, all_time: 0, by_category: {},
        by_stage: {}, by_source: {}}, blocked: [], presend_skips: 0,
        presend_examples: [], window_days: 7, database: 'ok'};
      if (path.startsWith('/api/brief')) {
        status = briefStatus;
        data = status === 200 ? briefPayload : {error: 'health check failed: boom'};
      }
      if (path.startsWith('/api/trends')) {
        status = trendsStatus;
        data = status === 200 ? trendsPayload : {error: 'trends failed'};
      }
      if (path.startsWith('/api/health')) {
        status = healthStatus;
        data = status === 200 ? healthPayload : {error: 'health engine crashed'};
      }
      if (path.startsWith('/api/pulse')) {
        status = pulseStatus;
        data = status === 200 ? pulsePayload : {error: 'timeline failed'};
      }
      if (path.startsWith('/api/search')) {
        status = searchStatus;
        data = status === 200 ? searchPayload : {error: 'search failed'};
      }
      if (path.startsWith('/api/trace')) {
        status = traceStatus;
        data = status === 200 ? tracePayload : {error: 'trace failed'};
      }
      if (path.startsWith('/api/scorecard')) {
        status = scoreStatus;
        data = status === 200 ? scorePayload : {error: 'scorecard failed'};
      }
      return {ok: status === 200, status, json: async () => data};
    };
  }});

(async () => {
  const w = dom.window, d = w.document;
  w.eval("TOKEN='test'");
  await w.refresh();
  await new Promise(resolve => setTimeout(resolve, 30));

  // ---- overview: one ranked list, every row with its fix -----------------
  const headline = d.getElementById('ovheadline').textContent;
  assert.match(headline, /2 things need you/, 'the brief says how much needs you');
  assert.match(d.getElementById('ovpill').textContent, /FAULT/,
    'one fault is enough for the pill to say FAULT');
  const attention = [...d.querySelectorAll('#ovattention li.attn')];
  assert.equal(attention.length, 2, 'one row per ranked item');
  assert.equal(attention[0].className.includes('error'), true,
    'the fault is first, whatever order the server sent');
  assert.ok(attention[0].querySelector('.attnfix code'), 'the fix is in the row');
  assert.equal(attention[0].querySelector('a').getAttribute('href'), '#categories',
    'and it links to the panel that explains it');
  assert.ok(!d.getElementById('ovattention').querySelector('img'),
    'a check detail cannot inject markup into the brief either');
  let fixCopied = '';
  w.navigator.clipboard = {writeText: async text => { fixCopied = text; }};
  d.querySelector('#ovattention .attnfix button').click();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.match(fixCopied, /systemctl restart bestgaa/, 'the copy button copies the command');
  assert.match(d.getElementById('ovsub').textContent, /rules v5 installed, bot running v4/,
    'installed vs running rules are named, not guessed');

  // ---- health -----------------------------------------------------------
  const line = d.getElementById('healthline').textContent;
  assert.match(line, /WARN/, 'the overall state is spelled out');
  assert.match(line, /9 ok/, 'the counts are shown');
  assert.match(line, /FloodWait/, 'the headline names the first fault');
  const checks = d.querySelectorAll('#healthrows .check');
  assert.equal(checks.length, 3, 'one tile per check');
  assert.match(d.getElementById('healthrows').textContent, /Failed posts/);
  assert.match(d.getElementById('healthrows').textContent,
    /Open the trace for one of them/, 'a fault carries its fix');
  assert.match(d.getElementById('healthrows').textContent, /UNKNOWN/,
    'an unanswerable check says UNKNOWN, not OK');
  assert.ok(!d.getElementById('healthrows').querySelector('img'),
    'a check detail cannot inject markup');
  // ask systemd: the button must add ?services=1
  w.eval("showServices = true");
  await w.loadHealth();
  assert.ok(calls.some(c => c.path === '/api/health?services=1'),
    'the systemd switch is honoured');
  w.eval("showServices = false");

  // the incident summary is built from what the panel already has
  assert.match(d.getElementById('consoleupdated').textContent, /checks ran at/,
    'the panel says when it last ran');
  let copied = '';
  w.navigator.clipboard = {writeText: async text => { copied = text; }};
  await w.copyHealthReport();
  assert.match(copied, /overall WARN/);
  assert.match(copied, /\[WARN\] Failed posts: 1 failed · FloodWait 30s/,
    'the summary carries the fault and its detail');
  assert.match(copied, /fix: Open the trace/,
    'the summary carries the fix, so it can be pasted into a report');
  assert.ok(!copied.includes('<img'), 'and it is plain text');

  // ---- pulse ------------------------------------------------------------
  const pulseCounts = d.getElementById('pulselast').textContent;
  assert.match(pulseCounts, /intake/);
  assert.match(pulseCounts, /last 3m ago/, 'the age of the last event of each kind');
  const events = d.querySelectorAll('#pulserows tr');
  assert.equal(events.length, 3, 'one row per event');
  const pulseText = d.getElementById('pulserows').textContent;
  assert.match(pulseText, /refused · presend · gambling_betting/);
  assert.match(pulseText, /link converted/);
  assert.match(pulseText, /Samsung neo qled/);
  assert.ok(!d.getElementById('pulserows').querySelector('img'),
    'an event detail cannot inject markup');
  assert.match(d.getElementById('pulsenote').textContent, /silence: send 5m/,
    'a silence is called out, not left to be noticed');
  // the kind filter is sent to the server
  d.getElementById('pulsekind').value = 'block';
  await w.loadPulse();
  assert.ok(calls.some(c => c.path.includes('/api/pulse') && c.path.includes('kind=block')),
    'the kind filter goes to the endpoint');

  // ---- the throughput chart ---------------------------------------------
  const columns = d.querySelectorAll('#pulsechart .col');
  assert.equal(columns.length, 3, 'one bar per IST hour');
  assert.equal(columns[2].querySelectorAll('i').length, 5,
    'the busiest hour stacks every kind that happened in it');
  assert.equal(columns[0].querySelectorAll('i').length, 0, 'an empty hour is an empty bar');
  assert.match(columns[2].innerHTML, /title="send: 2"/);
  assert.match(d.getElementById('pulsechart').textContent, /19/, 'the hour is labelled');

  // ---- the trend panel: this window against the one before --------------
  await w.loadTrends();
  const tiles = [...d.querySelectorAll('#ovtiles .ovtile')];
  assert.equal(tiles.length, 5, 'five headline metrics');
  assert.match(tiles[0].textContent, /Sent/, 'the first tile is sends');
  assert.match(tiles[0].textContent, /6,800/, 'with the real number, grouped');
  assert.match(tiles[0].querySelector('.chip').className, /bad/,
    'sends down is a bad movement, coloured as such');
  assert.match(tiles[0].querySelector('.chip').textContent, /▼ 0\.9%/,
    'the delta is a percentage with its direction');
  assert.equal(d.querySelectorAll('#ovtiles svg.spark').length, 4,
    'every windowed tile draws its own sparkline (the backlog is a snapshot)');
  assert.match(d.querySelectorAll('#ovtiles .ovtile')[4].textContent, /no bars|snapshot/,
    'the snapshot tile does not pretend to have a trend');
  assert.match(d.querySelectorAll('#ovtiles svg.spark')[0].innerHTML, /stroke-dasharray/,
    'the window before is drawn as a dashed baseline');
  const metricRows = d.querySelectorAll('#ovmetrics .mrow');
  assert.equal(metricRows.length, 7, 'every metric, not just the tiles');
  assert.match(metricRows[6].textContent, /Queued now/);
  assert.match(metricRows[6].textContent, /snapshot/,
    'the backlog is a snapshot and says so instead of inventing a delta');
  assert.equal(d.querySelectorAll('#ovchannels tr').length, 2, 'per-channel split');
  assert.ok(!d.getElementById('ovchannels').querySelector('img'),
    'a channel name from the database cannot inject markup');
  assert.equal(d.querySelectorAll('#ovreasons tr').length, 2, 'top skip reasons');
  assert.match(d.getElementById('ovstamp').textContent, /whole window/,
    'the panel says where the numbers come from');
  assert.ok(!/NaN/.test(d.getElementById('overview').textContent), 'no NaN anywhere');

  // ---- exports are a URL to the same rows --------------------------------
  let opened = '';
  w.open = url => { opened = url; return null; };
  d.getElementById('pulsehours').value = '24';
  w.downloadExport('pulse', '24');
  assert.match(opened, /^\/api\/export\?what=pulse/);
  assert.match(opened, /hours=24/);
  assert.match(opened, /token=test/, 'a download carries the token in the query');
  d.getElementById('qsearch').value = 'qled';
  w.downloadExport('search');
  assert.match(opened, /what=search/);
  assert.match(opened, /q=qled/);
  w.downloadExport('scorecard', '30');
  assert.match(opened, /what=scorecard&token=test&days=30/);

  // ---- a timeline queue id opens the trace ------------------------------
  const traceButton = [...d.querySelectorAll('#pulserows button')]
    .find(b => b.textContent.trim() === '#3');
  assert.ok(traceButton, 'each event with a post offers a trace button');
  traceButton.click();
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.ok(calls.some(c => c.path === '/api/trace?queue_id=3'),
    'clicking it asks for that post');
  assert.equal(d.getElementById('traceid').value, '3', 'and fills the input');

  // ---- trace ------------------------------------------------------------
  d.getElementById('traceid').value = '1';
  await w.tracePost();
  const head = d.getElementById('tracehead').textContent;
  assert.match(head, /#1/);
  assert.match(head, /DealSauce/);
  assert.match(head, /Samsung neo qled/, 'the post itself is shown');
  assert.match(d.getElementById('tracefacts').textContent, /₹60,000/,
    'the price the bot routed on');
  assert.match(d.getElementById('tracefacts').textContent, /electronics/);
  const rows = d.querySelectorAll('#tracerows tr');
  assert.equal(rows.length, 3, 'one row per predicted/actual target');
  const rowText = d.getElementById('tracerows').textContent;
  assert.match(rowText, /delivered/);
  assert.match(rowText, /skipped/);
  assert.match(rowText, /not attempted/,
    'a planned-but-never-attempted target is called out');
  assert.match(d.getElementById('traceextras').textContent, /bitli\.in\/neoQLED55/,
    'the delivered link is shown');
  assert.match(d.getElementById('traceextras').textContent, /write-before-send fence/);
  assert.ok(!d.getElementById('tracerows').querySelector('img'),
    'a skip reason cannot inject markup');

  // a bad id never reaches the network
  const before = calls.filter(c => c.path.startsWith('/api/trace')).length;
  d.getElementById('traceid').value = 'abc';
  await w.tracePost();
  assert.equal(calls.filter(c => c.path.startsWith('/api/trace')).length, before,
    'a non-numeric id is refused locally');
  assert.match(d.getElementById('traceerror').textContent, /numeric queue id/);

  // ---- search -----------------------------------------------------------
  d.getElementById('qsearch').value = 'qled';
  await w.runSearch();
  const qrows = d.querySelectorAll('#qrows tr');
  assert.equal(qrows.length, 2, 'one row per hit');
  const qtext = d.getElementById('qrows').textContent;
  assert.match(qtext, /post/);
  assert.match(qtext, /refusal/);
  assert.match(qtext, /qled/);
  assert.ok(!d.getElementById('qrows').querySelector('img'), 'a snippet cannot inject markup');
  // one character is refused without a request
  const searchCalls = calls.filter(c => c.path.startsWith('/api/search')).length;
  d.getElementById('qsearch').value = 'q';
  await w.runSearch();
  assert.equal(calls.filter(c => c.path.startsWith('/api/search')).length, searchCalls,
    'a one-character search is not sent');
  assert.match(d.getElementById('qrows').textContent, /at least 2 characters/);

  // ---- scorecard --------------------------------------------------------
  const scRows = d.querySelectorAll('#scchannels tr');
  assert.equal(scRows.length, 2, 'one row per channel');
  const scText = d.getElementById('scchannels').textContent;
  assert.match(scText, /LootZoneIndia11/);
  assert.match(scText, /80%/, 'the delivered share is shown');
  assert.match(scText, /19:00/, 'the best hour is shown in IST');
  assert.match(scText, /never/, 'a channel that never received says so');
  assert.match(d.getElementById('scsources').textContent, /DealSauce/);
  assert.ok(!d.getElementById('scsources').querySelector('img'),
    'a source name cannot inject markup');
  assert.match(d.getElementById('sctotals').textContent, /7/, 'totals reconcile');

  // ---- the 5 s tick does not re-run the expensive books ----------------
  calls.length = 0;
  await w.refresh(false);
  const light = calls.map(c => c.path);
  assert.ok(light.includes('/api/status'), 'the light tick still refreshes status');
  assert.ok(light.some(p => p.startsWith('/api/pulse')),
    'the light tick still refreshes the timeline');
  assert.ok(light.some(p => p.startsWith('/api/brief')),
    'the light tick refreshes the attention list');
  assert.ok(!light.some(p => p.startsWith('/api/health')),
    'and does not ask for health twice: the brief already carries it');
  assert.ok(!light.some(p => p.startsWith('/api/trends')),
    'trends are too heavy for 5 s and stay on the slower tick');
  for (const heavy of ['/api/audit?limit=200', '/api/categories?hours=24',
    '/api/link-conversion?limit=5', '/api/scorecard?days=7']) {
    assert.ok(!light.some(p => p.startsWith(heavy.split('?')[0])),
      `the light tick skips ${heavy}`);
  }
  calls.length = 0;
  await w.refresh();
  const full = calls.map(c => c.path);
  assert.ok(full.some(p => p.startsWith('/api/audit')), 'a full refresh still runs the audit');
  assert.ok(full.some(p => p.startsWith('/api/categories')), 'and the category books');
  assert.ok(full.some(p => p.startsWith('/api/trends?hours=')),
    'the full tick recomputes the window comparison');

  // ---- dead endpoints cannot take the page down -------------------------
  healthStatus = 500; pulseStatus = 500; scoreStatus = 500; searchStatus = 500;
  briefStatus = 500; trendsStatus = 500;
  await w.loadBrief();
  assert.match(d.getElementById('ovheadline').textContent, /brief unavailable/,
    'a dead brief says so instead of showing stale confidence');
  await w.loadTrends();
  assert.match(d.getElementById('ovmetrics').textContent, /trends unavailable/);
  briefStatus = 200; trendsStatus = 200;
  await w.loadHealth();
  await w.loadPulse();
  await w.loadScorecard();
  d.getElementById('qsearch').value = 'qled';
  await w.runSearch();
  assert.match(d.getElementById('healthline').textContent, /health unavailable/);
  assert.match(d.getElementById('pulserows').textContent, /timeline unavailable/);
  assert.match(d.getElementById('scchannels').textContent, /scorecard unavailable/);
  assert.match(d.getElementById('qrows').textContent, /search failed/);
  // ...and the rest of the dashboard still renders
  await w.refresh();
  assert.match(d.getElementById('stats').textContent, /sent today/,
    'the status card survives four dead endpoints');

  // ---- trace failure keeps the last good trace visible ------------------
  traceStatus = 500;
  d.getElementById('traceid').value = '1';
  await w.tracePost();
  assert.match(d.getElementById('traceerror').textContent, /trace failed/);

  assert.equal(w.__pwned, undefined, 'nothing executed');
  console.log('Ops console DOM PASS: a ranked attention list where every row carries '
    + 'its fix, a trend panel comparing this window with the one before (dashed '
    + 'baseline, per channel, per skip reason), health tiles with fixes and UNKNOWN '
    + 'states rendered from the same brief, a merged timeline with kinds and '
    + 'silences, trace-posting from an event, predicted-vs-actual including '
    + 'not-attempted, search with local refusal, per-channel and per-source '
    + 'scorecards, escaped source text, and dead endpoints that cannot blank the page.');
  process.exit(0);   // the page refreshes on a timer; the test is done
})().catch(error => {
  console.error(error);
  process.exit(1);
});
