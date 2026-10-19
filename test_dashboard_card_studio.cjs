// Card studio DOM tests: the panel renders the bot's reading of a caption, the
// caption per surface, and the forward plan - and a hostile caption or a dead
// endpoint can neither execute code nor blank the rest of the dashboard.
// The API is mocked; no bot, server, network, database or channel is touched.
const { JSDOM } = require('jsdom');
const { execFileSync } = require('node:child_process');
const assert = require('node:assert/strict');
const html = execFileSync(process.env.PYTHON || 'python3',
  ['-c', 'from dashboard.app import UI_HTML; print(UI_HTML)'], {encoding: 'utf8'});

// A caption, a destination name and a link verdict reason an attacker controls.
const EVIL = '<img src=x onerror="window.__pwned=1">';
const calls = [];
let studioStatus = 200, pngStatus = 200;

const CAPTION = [
  '🔥 Headphone Bluetooth Neckband Wireless Sport In-ear Earphone with Mic',
  'Deal ₹78  M.R.P.: ₹120  (35% off)',
  'Apply 4% coupon at checkout',
  'Min Buy Qnty = 2',
].join('\n');

function studio() {
  return {
    facts: {title: '🔥 Headphone Bluetooth Neckband Wireless Sport In-ear Earphone',
            price: 78, mrp: 120, discount: 35, coupon: {percent: 4, amount: null, code: null},
            coupon_saving: 3, final: 75, min_qty: 2, lowest_price_claim: true},
    card: {available: true, why: '', width: 1080, height: 609, bytes: 41000},
    link: {url: 'https://amzn.lt/' + EVIL, owned: false, bucket: 'earns_nothing',
           route: 'passthrough', why: 'this link earns nothing for us ' + EVIL},
    captions: {
      telegram: '🔥 Headphone Bluetooth Neckband @ ₹75\n\nApply 4% coupon (saves ₹3.12)\n\n🔗 link withheld',
      whatsapp: 'Headphone Bluetooth Neckband @ ₹75\nApply 4% coupon (saves ₹3.12)\nlink withheld',
      premium: '🔥 Headphone Bluetooth Neckband\n₹75 · MRP ₹120 · 35% off',
      text: 'Headphone Bluetooth Neckband @ ₹75\nlink withheld'},
    plan: {
      category: 'electronics', category_reason: 'matched', evidence: [], rules_version: 3,
      accepted: 2, card_destinations: 2, link_ok: false,
      rows: [
        {destination: 'mainloots' + EVIL, surface: 'telegram', kind: 'main', enabled: true,
         accepts: true, format: 'card + caption', why: 'unfiltered target ' + EVIL,
         carrier: 'send_file(entity, card.png, caption=…)'},
        {destination: 'electroshelf', surface: 'telegram', kind: 'shelf', enabled: true,
         accepts: true, format: 'card + caption', why: 'shelf keeps electronics',
         carrier: 'send_file(entity, card.png, caption=…)'},
        {destination: 'offshelf', surface: 'telegram', kind: 'shelf', enabled: false,
         accepts: false, format: 'not sent', why: 'channel disabled', carrier: ''},
        {destination: '12034567890@g.us', surface: 'whatsapp', kind: 'channel', enabled: true,
         accepts: true, format: 'card + caption', why: 'WA_CHANNEL_ELECTRONICS',
         carrier: 'broadcastMediaItem(sock, job, item, caption)'},
      ],
      warnings: ['link not printable: ' + EVIL],
      declared_targets: {electronics: 'electroshelf'}},
    surfaces: ['telegram', 'whatsapp', 'premium', 'text'],
    buttons: [{label: 'Join Shpsy Loots \u2764\ufe0f', url: 'https://t.me/shpsyloots'}],
    button_note: 'Telegram posts carry this button row; WhatsApp channels have no inline buttons.',
    db_exists: true, preview_only: true,
  };
}

let savedButtons = [{label: 'Join Shpsy Loots \u2764\ufe0f', url: 'https://t.me/shpsyloots'}];
let putBodies = [];
const RECENT = {jobs: [{id: 7, source: 'lootsdaily', status: 'done', sends: 2, skips: 0,
  held: 0, error: '', snippet: CAPTION, link: 'https://amzn.lt/ours'}]};

const dom = new JSDOM(html, {url: 'https://preview.example/', runScripts: 'dangerously', beforeParse(w) {
  w.confirm = () => true; w.alert = message => { throw new Error(message); };
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  // jsdom does not implement object URLs; the page only needs them to display a
  // fetched blob, so a deterministic stand-in is enough here.
  let blobs = 0;
  w.URL.createObjectURL = () => 'blob:mock/' + (++blobs);
  w.URL.revokeObjectURL = () => {};
  w.fetch = async (path, options) => {
    calls.push({path, method: (options && options.method) || 'GET'});
    let data = {}, status = 200;
    if (path === '/api/status') data = {queue: {pending: 1}, db_exists: true, today_sends: 4};
    if (path === '/api/channels') data = {revision: 'r1', dashboard: [], builtin: [],
      categories: {electronics: 'Electronics'}, sources: ['deals'],
      category_rules_version: 3, bot_category_rules_version: 3, activation: {}};
    if (path === '/api/recent?limit=15') data = {jobs: []};
    if (path === '/api/recent?limit=25') return {ok: true, status: 200,
      json: async () => RECENT};
    if (path === '/api/audit?limit=200') data = {posts_checked: 0, link_summary: {},
      delivery_summary: {}, findings_by_kind: {}};
    if (path.startsWith('/api/categories')) data = {categories: [], findings: [], totals: {},
      money_totals: null, whatsapp: null, main: null, recent: {}};
    if (path.startsWith('/api/card-studio')) {
      status = studioStatus;
      data = status === 200 ? studio() : {error: 'card studio failed'};
      return {ok: status === 200, status, json: async () => data};
    }
    if (path.startsWith('/api/post-buttons')) {
      const method = (options && options.method) || 'GET';
      if (method === 'GET') {
        return {ok: true, status: 200, json: async () => ({buttons: savedButtons,
          presets: [{label: 'Join Main Loots \u2764\ufe0f', url: 'https://t.me/mainloots'},
                    {label: 'Under 99rs \ud83e\udd1f', url: 'https://t.me/under99rs'}],
          max_buttons: 2, max_label: 40, allowed_hosts: ['t.me'], path: '/x/post_buttons.json'})};
      }
      const body = options && options.body ? JSON.parse(options.body) : {};
      putBodies.push(body);
      const bad = (body.buttons || []).some(b => !(b.url || '').startsWith('https://t.me/'));
      if (bad) return {ok: false, status: 400,
        json: async () => ({errors: ['button 2 url host is not a Telegram host']})};
      savedButtons = body.buttons || [];
      return {ok: true, status: 200, json: async () => ({saved: true, buttons: savedButtons})};
    }
    if (path.startsWith('/api/card.png')) {
      status = pngStatus;
      if (status !== 200) return {ok: false, status, json: async () => ({error: 'no pillow'})};
      return {ok: true, status, blob: async () => new w.Blob([new Uint8Array([1, 2, 3])],
        {type: 'image/png'})};
    }
    return {ok: status === 200, status, json: async () => data};
  };
}});

(async () => {
  const w = dom.window, d = w.document;
  w.eval("TOKEN='test'");
  await w.refresh();

  const box = d.getElementById('cardtext');
  const factsBox = d.getElementById('cardfacts');

  // --- nothing typed yet: no fetch, a hint instead ---------------------------
  await w.loadStudio(true);
  assert.match(factsBox.textContent, /paste a deal caption first/,
    'an empty caption is answered with a hint, not a request');
  assert.equal(calls.filter(c => c.path.startsWith('/api/card-studio')).length, 0,
    'no request goes out for an empty caption');

  // --- a real render ----------------------------------------------------------
  box.value = CAPTION;
  d.getElementById('cardlink').value = 'https://amzn.lt/abc';
  await w.loadStudio(true);
  assert.equal(calls.filter(c => c.path.startsWith('/api/card-studio')).length, 1,
    'the studio payload is fetched once per render');
  assert.match(calls.find(c => c.path.startsWith('/api/card-studio')).path, /text=/,
    'the caption travels in the query string');
  assert.match(factsBox.textContent, /price Rs78/, 'the deal price is shown');
  assert.match(factsBox.textContent, /M\.R\.P\. Rs120/, 'the struck M.R.P. is shown');
  assert.match(factsBox.textContent, /35% off/, 'the discount is shown');
  assert.match(factsBox.textContent, /final Rs75/, 'the final price is shown');
  assert.match(factsBox.textContent, /coupon 4%/, 'the coupon is shown');
  assert.match(factsBox.textContent, /category electronics/, 'the classification is shown');

  const caption = d.getElementById('cardcaption');
  assert.equal(caption.textContent, studio().captions.telegram,
    'the caption is the endpoint text, verbatim');
  assert.equal(caption.children.length, 0, 'and it is text, never markup');
  d.getElementById('cardsurface').value = 'whatsapp';
  await w.loadStudio(false);
  assert.equal(caption.textContent, studio().captions.whatsapp,
    'switching the surface swaps the caption without redrawing the card');
  assert.match(d.getElementById('cardsurfacename').textContent, /whatsapp/,
    'the caption block says which surface it is');

  // --- the card image ---------------------------------------------------------
  const img = d.getElementById('cardimg');
  assert.ok(img.src.startsWith('blob:'), 'the PNG arrives through the tokened fetch, not a bare URL');
  assert.equal(img.style.display, 'block', 'and it is shown once decoded');

  // --- the forward plan -------------------------------------------------------
  const planRows = d.querySelectorAll('#cardplan tr');
  assert.equal(planRows.length, 4, 'one row per destination');
  assert.equal(d.querySelectorAll('#cardplan .badge.ok').length, 3,
    'the three accepting destinations say card + caption');
  assert.match(planRows[2].textContent, /not sent/, 'a skipped destination is not dressed up');
  assert.match(planRows[2].textContent, /disabled/, 'and a disabled channel is labelled');
  assert.ok(planRows[0].textContent.includes('mainloots<img src=x'),
    'a hostile destination name stays text');

  // --- warnings and a link we cannot print ------------------------------------
  const warn = d.getElementById('cardwarn');
  assert.match(warn.textContent, /link not printable/, 'the link warning is shown');
  assert.match(warn.textContent, /this link earns nothing for us/, 'and the verdict reason');
  assert.equal(d.querySelectorAll('#cardstudio img').length, 1,
    'the only image in the panel is the card itself');
  assert.equal(w.__pwned, undefined, 'nothing injected executed');

  // --- a dead endpoint cannot blank the dashboard ------------------------------
  pngStatus = 503;
  await w.loadStudio(true);
  assert.match(d.getElementById('cardimgerr').textContent, /card image unavailable/,
    'a failing PNG endpoint says so next to the frame, and keeps the plan');
  assert.equal(d.querySelectorAll('#cardplan tr').length, 4,
    'the plan survives a missing PNG');
  pngStatus = 200; studioStatus = 500;
  await w.loadStudio(true);
  assert.match(d.getElementById('cardplan').textContent, /card studio unavailable/,
    'a failing studio endpoint says so in the panel');
  assert.ok(d.getElementById('stats').textContent.length > 0,
    'the rest of the dashboard still rendered');
  studioStatus = 200; pngStatus = 200;

  // --- the button row: editor, mock preview, and a refused save --------------
  await w.loadButtons();
  const rowsBox = d.getElementById('btnrows');
  assert.equal(rowsBox.querySelectorAll('input').length, 2,
    'the saved row loads into the editor as label+url inputs');
  assert.equal(rowsBox.querySelectorAll('input')[0].value, 'Join Shpsy Loots \u2764\ufe0f',
    'the label is the saved text, not markup');
  assert.match(d.getElementById('btnstate').textContent, /configured: 1 button/,
    'the editor says what is configured');
  assert.equal(d.querySelectorAll('#cardbtns a').length, 1,
    'the studio preview draws the button row under the card');
  const mock = d.querySelector('#cardbtns a');
  assert.equal(mock.getAttribute('href'), 'https://t.me/shpsyloots', 'the mock links where the post would');
  assert.match(mock.getAttribute('rel') || '', /noopener/, 'and opens safely');
  await w.loadStudio(true);
  assert.equal(d.querySelectorAll('#cardbtns a').length, 1,
    'a re-render keeps the configured row');
  w.addButtonRow();
  assert.equal(rowsBox.querySelectorAll('input').length, 4, 'a second button can be added');
  w.addButtonRow();
  assert.equal(rowsBox.querySelectorAll('input').length, 4, 'a third cannot');
  assert.equal(d.getElementById('btnadd').style.display, 'none', 'and the add button hides');
  const inputs = rowsBox.querySelectorAll('input');
  inputs[2].value = 'Evil'; inputs[3].value = 'https://evil.example/join';
  inputs[2].dispatchEvent(new w.Event('input')); inputs[3].dispatchEvent(new w.Event('input'));
  await w.saveButtons();
  assert.match(d.getElementById('btnstate').textContent, /NOT saved/,
    'a non-Telegram button is refused with the server reason');
  assert.equal(putBodies.length, 1, 'the refusal came from a real save attempt');
  inputs[3].value = 'https://t.me/under99rs';
  inputs[3].dispatchEvent(new w.Event('input'));
  inputs[2].value = 'Under 99rs Loot Deals';
  inputs[2].dispatchEvent(new w.Event('input'));
  await w.saveButtons();
  assert.match(d.getElementById('btnstate').textContent, /saved 2 button/,
    'two Telegram buttons save');
  assert.deepEqual(putBodies[1].buttons.map(b => b.url),
    ['https://t.me/shpsyloots', 'https://t.me/under99rs'], 'the PUT carries exactly the two rows');
  assert.equal(d.querySelectorAll('#cardbtns a').length, 2,
    'and the preview under the card updates immediately');
  savedButtons = [{label: EVIL, url: 'https://t.me/x'}];
  await w.loadButtons();
  assert.equal(rowsBox.querySelectorAll('img').length, 0,
    'a hostile saved label stays input text, never markup');
  assert.ok(rowsBox.querySelectorAll('input')[0].value.includes('<img src=x'),
    'and sits in the input value as plain text');

  // --- cross-channel: load a real post into the studio in one click ----------
  await w.loadRecentForStudio();
  const recent = d.getElementById('cardrecent');
  assert.equal(recent.querySelectorAll('option').length, 2,
    'recent posts are offered to the studio');
  assert.match(recent.querySelectorAll('option')[1].textContent, /lootsdaily/,
    'each option names its source channel');
  recent.value = '7';
  recent.dispatchEvent(new w.Event('change'));
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(d.getElementById('cardtext').value, CAPTION,
    'picking a post loads its caption');
  assert.equal(d.getElementById('cardlink').value, 'https://amzn.lt/ours',
    'and the link it carried, so the provenance verdict is real');

  // --- presets: one click adds one of OUR channels ----------------------------
  const preset = d.getElementById('btnpreset');
  assert.equal(preset.querySelectorAll('option').length, 3,
    'our channels and the folder are offered as presets');
  w.eval("buttonRows = [{label: 'x', url: 'https://t.me/x'}]; renderButtonRows();");
  preset.value = '0';
  preset.onchange();
  assert.equal(w.eval("buttonRows.length"), 2, 'a preset joins the row without typing');
  assert.match(d.getElementById('btnstate').textContent, /press Save buttons/,
    'and the editor says it is not published yet');
  preset.value = '1';
  preset.onchange();
  assert.match(d.getElementById('btnstate').textContent, /row is full/,
    'a third button is refused before it reaches Telegram');

  console.log('Card studio DOM PASS: facts read from the bot own parsers, captions verbatim per '
    + 'surface, tokened PNG through a blob URL, forward plan with accept/skip reasons, hostile '
    + 'text kept as text, a configurable two-button join row with one-click presets and a '
    + 'refused non-Telegram save, a cross-channel picker that loads a real post, '
    + 'and a dead endpoint that cannot blank the dashboard.');
  w.close();
  process.exit(0);
})().catch(error => { console.error(error); process.exit(1); });
