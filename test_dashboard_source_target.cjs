// Source → Target + Safety panels, in a real DOM.
//
// The two panels an operator uses to answer "why did this post / not post?" are
// driven here with the API mocked: the reported Samsung QLED post must render
// ₹60,000 (not ₹60), the electronics shelf, the channels that receive it and the
// ones that withhold it — with reasons — and every string that came from a
// source channel must stay TEXT. A dead /api/source-to-target or /api/safety
// must never blank the rest of the dashboard.
const { JSDOM } = require('jsdom');
const { execFileSync } = require('node:child_process');
const assert = require('node:assert/strict');

const html = execFileSync(process.env.PYTHON || 'python3',
  ['-c', 'from dashboard.app import UI_HTML; print(UI_HTML)'], {encoding: 'utf8'});

const EVIL = '<img src=x onerror="window.__pwned=1">';
const calls = [];
let plannerStatus = 200;
let safetyStatus = 200;

// The real shape of GET /api/safety (see dashboard/app.py): totals, breakdowns,
// refusals with a masked excerpt, the live policy.
const safetyPayload = {
  policy: {version: '2026-10-19.4', enabled: true, strict_review: false,
           allow_intimate: false, allow_suggestive: false,
           categories: [{key: 'sexual_explicit', label: 'Explicit sexual content',
                         severity: 'block', why: 'public channels', patterns: 32, domains: 0},
                        {key: 'gambling_betting', label: 'Gambling / betting',
                         severity: 'block', why: 'not a deal', patterns: 18, domains: 12}]},
  window_days: 7,
  totals: {today: 2, window: 2, all_time: 5,
           by_category: {sexual_explicit: 1, gambling_betting: 1},
           by_stage: {intake: 1, presend: 1}, by_source: {TrickXpert: 1, Offerzone_deals: 1}},
  blocked: [
    {stage: 'presend', source: 'Offerzone_deals' + EVIL, msg_id: 42, queue_id: 3,
     target: 'LootZoneIndia11', category: 'gambling_betting', label: 'Gambling / betting',
     matched: '1xbet.com', reason: 'Betting spam is not a deal ' + EVIL,
     policy_version: '2026-10-19.4', excerpt: '******** bonus join now', created_at: 1792480000},
    {stage: 'intake', source: 'TrickXpert', msg_id: 41, queue_id: null, target: '',
     category: 'sexual_explicit', label: 'Explicit sexual content', matched: 'sex',
     reason: 'Our channels are public', policy_version: '2026-10-19.4',
     excerpt: '*** video leaked', created_at: 1792470000},
  ],
  presend_skips: 1, presend_examples: [], database: 'ok',
};

// The real shape of POST /api/source-to-target for the operator's post.
const plannerPayload = {
  input: {text: 'Samsung neo qled 55” @ ₹60k\nhttps://link.amazon/B0gWibFuA',
          source: 'DealSauce', chars: 57, links_found: 1},
  facts: {price: 60000, mrp: null, discount: null, coupon: {}, final: 60000,
          title: 'Samsung neo qled 55” @ ₹60k' + EVIL, quality_score: 0,
          premium_eligible: false},
  safety: {allowed: true, severity: 'clean', category: '', label: '', reason: '',
           version: '2026-10-19.4'},
  links: [{url: 'https://link.amazon/B0gWibFuA', host: 'link.amazon', role: 'amazon_short',
           why: 'Amazon short wrapper: followed to the real /dp/ page, our tag applied, '
                + 'shortened with our shortener ' + EVIL,
           product_id: '', published_as: 'our converted link (verified at send)',
           never_published_as: true}],
  category: {key: 'electronics', label: 'Electronics', reason: 'matched',
             evidence: [{category: 'electronics', phrase: 'neo qled', weight: 6, source: 'text'}],
             scores: {electronics: 6}, accepted_by: ['ElectroShelf'],
             withheld_by: [{name: 'FashionShelf', why: 'takes fashion'}]},
  identity: {product_ids: [], note: 'the product id only exists after the bot resolves it'},
  plan: {
    known_source: true,
    targets: [{name: 'LootZoneIndia11', phase: 'source route', why: 'this source is routed here'},
              {name: 'PowerLoots1', phase: 'source route', why: 'this source is routed here'},
              {name: 'ElectroShelf', phase: 'shelf', why: 'a electronics shelf'}],
    excluded: [{name: 'Under99Deals11', why: '₹60000 is above ₹99'},
               {name: 'under499loots', why: '₹60000 is above ₹499'},
               {name: 'FashionShelf', why: 'withholds this post: the shelf takes fashion ' + EVIL}],
    notes: ['predicted from the bot\'s published routing map'],
  },
  warnings: ['a warning about the source ' + EVIL],
  config_revision: 'r1',
  policy: {version: '2026-10-19.4', enabled: true, categories: []},
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
        categories: {electronics: 'Electronics', fashion: 'Clothing'},
        sources: ['DealSauce', 'TrickXpert'], category_rules_version: 4,
        bot_category_rules_version: 4, activation: {}};
      if (path === '/api/recent?limit=15') data = {jobs: []};
      if (path === '/api/audit?limit=200') data = {posts_checked: 3, link_summary: {},
        delivery_summary: {}, findings_by_kind: {}};
      if (path.startsWith('/api/link-conversion')) data = {totals: {}, rows: [], trend: []};
      if (path.startsWith('/api/post-buttons')) data = {buttons: [], max_buttons: 2};
      // A dead categories endpoint must not take the two new panels with it.
      if (path.startsWith('/api/categories')) {
        status = 500;
        data = {error: 'database not readable yet'};
      }
      if (path.startsWith('/api/safety')) {
        status = safetyStatus;
        data = status === 200 ? safetyPayload : {error: 'safety view failed'};
      }
      if (path === '/api/source-to-target') {
        status = plannerStatus;
        data = status === 200 ? plannerPayload : {errors: ['could not analyse this post']};
      }
      return {ok: status === 200, status, json: async () => data};
    };
  }});

(async () => {
  const w = dom.window, d = w.document;
  w.eval("TOKEN='test'");
  await w.refresh();

  // ---- Safety panel -------------------------------------------------------
  const safestats = d.getElementById('safetystats').textContent;
  assert.match(safestats, /2/, 'refusals today');
  assert.match(safestats, /5/, 'all-time refusals');
  assert.match(d.getElementById('safepolicy').textContent, /filter active/);
  assert.match(d.getElementById('safepolicy').textContent, /2026-10-19\.4/,
    'the panel states the policy it is enforcing');
  const safebars = d.getElementById('safebars').textContent;
  assert.match(safebars, /sexual_explicit/);
  assert.match(safebars, /gambling_betting/);
  const safetyrows = d.querySelectorAll('#safetyrows tr');
  assert.equal(safetyrows.length, 2, 'one row per refusal');
  const safetytext = d.getElementById('safetyrows').textContent;
  assert.match(safetytext, /\*\*\*\*\*\*\*\* bonus join now/, 'the matched term stays masked');
  assert.match(safetytext, /presend/, 'the stage that refused it');
  assert.match(safetytext, /intake/);
  assert.match(safetytext, /Our channels are public/, 'the human reason');
  assert.ok(!d.getElementById('safetyrows').querySelector('img'),
    'a source channel cannot inject markup through a refusal');
  assert.equal(w.__pwned, undefined);

  // ---- Source → Target: the reported post --------------------------------
  const text = d.getElementById('sttext'), source = d.getElementById('stsource');
  assert.ok(text && source, 'the planner inputs exist in the served page');
  assert.ok(d.getElementById('stsources'), 'the source datalist exists');
  assert.match(d.getElementById('stsources').innerHTML, /DealSauce/,
    'the source list is filled from /api/channels');
  text.value = 'Samsung neo qled 55” @ ₹60k\nhttps://link.amazon/B0gWibFuA';
  source.value = 'DealSauce';
  await w.analyseSource();
  const sent = calls.filter(c => c.path === '/api/source-to-target').pop();
  assert.deepEqual(sent.body, {text: text.value, source: 'DealSauce'},
    'the panel posts exactly what was pasted');
  assert.match(d.getElementById('stcard').textContent, /₹60,000/,
    'the price shown is the price the bot routes on');
  assert.match(d.getElementById('stcard').textContent, /electronics/);
  assert.match(d.getElementById('stfacts').textContent, /content polic/i);
  assert.match(d.getElementById('stlinks').textContent, /amazon_short/);
  assert.match(d.getElementById('stlinks').textContent, /never posted as-is/);
  const yes = d.getElementById('styes').textContent;
  assert.match(yes, /@LootZoneIndia11/);
  assert.match(yes, /@ElectroShelf/);
  const no = d.getElementById('stno').textContent;
  assert.match(no, /@Under99Deals11/);
  assert.match(no, /above ₹99/);
  assert.match(no, /@FashionShelf/);
  assert.match(d.getElementById('stnotes').textContent, /routing map/);
  assert.ok(!d.getElementById('stlinks').querySelector('img'),
    'the link explanation cannot inject markup');
  assert.ok(!d.getElementById('stno').querySelector('img'),
    'a withheld reason cannot inject markup');
  assert.equal(w.__pwned, undefined, 'nothing executed');

  // The example button is what a first-time operator clicks.
  w.loadSourceExample();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.match(text.value, /link\.amazon/);
  assert.equal(source.value, 'DealSauce');
  assert.equal(d.getElementById('sterror').textContent, '', 'no error after a good run');

  // ---- a dead planner endpoint says so and changes nothing else ----------
  plannerStatus = 500;
  d.getElementById('sttext').value = 'x https://link.amazon/abc';
  await w.analyseSource();
  assert.match(d.getElementById('sterror').textContent, /could not analyse/,
    'the failure is shown next to the button');
  assert.equal(d.getElementById('stfacts').textContent.includes('₹60,000'), true,
    'the last good analysis is not blanked by a failed one');
  assert.match(d.getElementById('safetystats').textContent, /2/,
    'a dead planner cannot blank the safety panel either');

  // ---- an empty box is refused locally, without a request ----------------
  const before = calls.filter(c => c.path === '/api/source-to-target').length;
  plannerStatus = 200;
  d.getElementById('sttext').value = '   ';
  await w.analyseSource();
  assert.equal(calls.filter(c => c.path === '/api/source-to-target').length, before,
    'no request for an empty post');
  assert.match(d.getElementById('sterror').textContent, /paste/i);

  console.log('Source → Target DOM PASS: ₹60,000 priced post, amazon_short wrapper, '
    + 'receiving/withheld channels with reasons, masked safety refusals, live policy, '
    + 'escaped source text, a dead endpoint that cannot blank the dashboard, '
    + 'and an empty post refused without a request.');
  process.exit(0);   // the page refreshes on a timer; the test is done
})().catch(error => {
  console.error(error);
  process.exit(1);
});
