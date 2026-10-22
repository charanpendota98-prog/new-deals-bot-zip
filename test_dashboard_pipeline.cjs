// Pipeline integrity panel in a real DOM: tiles, verdict, findings, per-source
// and per-channel tables, the trace drill-down, red-on-loss and hostile text.
// No Telegram, no network: /api/pipeline is mocked.
const {JSDOM}=require('jsdom');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const html=execFileSync(process.env.PYTHON||'python3',['-c','from dashboard.app import UI_HTML; print(UI_HTML)'],{encoding:'utf8'});

const calls=[];
let payload;
function loseOne(){
  payload={generated_at:1,window_hours:24,since_ist:'2026-10-22 00:00 IST',overall:'fail',
    verdict:'ALERT: 1 post(s) unaccounted for - 7 in = 1 posted + 2 withheld + 1 dropped (reason recorded) + 1 waiting + 1 failed + 1 unexplained',
    equation:'7 in = 1 posted + 2 withheld + 1 dropped (reason recorded) + 1 waiting + 1 failed',
    intake:7,unaccounted_ids:[7],
    tiles:[{key:'intake',label:'taken in',value:7,state:'ok'},
      {key:'posted',label:'posted',value:1,state:'ok'},
      {key:'withheld',label:'withheld (per channel)',value:2,state:'info'},
      {key:'dropped',label:'dropped (reason recorded)',value:1,state:'info'},
      {key:'waiting',label:'waiting',value:1,state:'info'},
      {key:'failed',label:'failed',value:1,state:'warn'},
      {key:'unaccounted',label:'UNACCOUNTED',value:1,state:'fail'}],
    first_send_seconds:{p50:40,p95:120,max:300},
    findings:[{key:'unaccounted',state:'fail',title:'1 intake(s) cannot be accounted for',
      detail:'No send, no recorded reason: queue 7',
      fix:'Open Trace for those queue ids; never let it sit.'},
      {key:'failed',state:'warn',title:'1 intake(s) failed',
      detail:'1× no verified affiliate link',fix:'Trace a failed queue id.'},
      {key:'silent_targets',state:'warn',title:'1 configured channel(s) received nothing',
      detail:'Under99Loots',fix:'Check Categories for its withhold reasons.'}],
    sources:[
      {source:'<img src=x onerror=alert(1)> deals',declared:true,watched:false,intake:3,posted:1,withheld:1,
       dropped:1,waiting:0,failed:0,unaccounted:0,verdict:'delivering',why:'posts are reaching channels',
       last_intake:'2026-10-22 12:00 IST',typical_gap_human:'9m',latency_p50:40,latency_p95:120,
       top_withhold_reason:''},
      {source:'SilentSource',declared:true,watched:true,intake:1,posted:0,withheld:0,dropped:0,
       waiting:0,failed:0,unaccounted:1,verdict:'unaccounted',
       why:'taken in, never posted, and no reason recorded',last_intake:'2026-10-22 11:00 IST',
       typical_gap_human:'1h',latency_p50:null,latency_p95:null,top_withhold_reason:''},
      {source:'QuietSource',declared:true,watched:false,intake:0,posted:0,withheld:0,dropped:0,
       waiting:0,failed:0,unaccounted:0,verdict:'silent',
       why:'configured, but nothing arrived in this window',last_intake:'—',
       typical_gap_human:'—',latency_p50:null,latency_p95:null,top_withhold_reason:''}],
    targets:[{target:'LootZoneIndia11',declared:true,sends:1,withheld:1,state:'receiving'},
      {target:'Under99Loots',declared:true,sends:0,withheld:0,state:'silent'}],
    declared_and_real:{declared:4,watched:1,seen:3,declared_but_silent:['QuietSource'],
      seen_but_not_declared:['TrickXpert']}};
}
loseOne();

const dom=new JSDOM(html,{url:'https://preview.example/',runScripts:'dangerously',beforeParse(w){
  w.alert=message=>{throw new Error(message)};
  w.HTMLElement.prototype.scrollIntoView=function(){};
  w.fetch=async(path,options={})=>{
    calls.push({path,method:options.method||'GET'});
    let data={},status=200;
    if(path==='/api/status')data={queue:{}};
    if(path==='/api/channels')data={revision:'r1',dashboard:[],categories:{},sources:[],source_catalog:[],source_limit:100};
    if(path==='/api/recent')data={jobs:[]};
    if(path.startsWith('/api/pipeline'))data=payload;
    if(path.startsWith('/api/trace'))data={queue_id:7,post:{},rows:[],extras:{}};
    return {ok:status===200,status,json:async()=>data};
  };
}});

(async()=>{
  const w=dom.window,d=w.document;
  w.eval("TOKEN='test'");
  await w.loadPipeline();

  // The window is asked for explicitly, and only once.
  assert.deepEqual(calls.map(c=>c.path),['/api/pipeline?hours=24']);
  assert.equal(d.getElementById('plhours').value,'24');

  // Tiles: every state is visible, and the lost one reads as a failure colour.
  const tiles=Array.from(d.querySelectorAll('#plinetiles .stat'));
  assert.equal(tiles.length,7);
  assert.match(tiles[0].textContent,/taken in/);
  assert.match(tiles[6].textContent,/UNACCOUNTED/);
  assert.equal(tiles[6].querySelector('strong').className,'bad');
  assert.equal(tiles[1].querySelector('strong').className,'ok');

  // The verdict is loud, states the equation, and offers the exact post.
  const verdict=d.getElementById('plineverdict');
  assert.match(verdict.textContent,/ALERT/);
  assert.match(verdict.textContent,/7 in = 1 posted/);
  assert.equal(verdict.className,'review-box badbox');
  const drill=verdict.querySelector('[data-trace]');
  assert.equal(drill.dataset.trace,'7');

  // Findings keep their state, their reason and their fix.
  const findings=Array.from(d.querySelectorAll('#plinefindings .finding'));
  assert.equal(findings.length,3);
  assert.match(findings[0].textContent,/cannot be accounted for/);
  assert.match(findings[0].textContent,/queue 7/);
  assert.match(findings[0].textContent,/fix: Open Trace/);

  // Per-source: unaccounted is a red cell, a watched source is marked, and a
  // hostile source name stays text (no element is created from it).
  const rows=Array.from(d.querySelectorAll('#plinesources tr'));
  assert.equal(rows.length,3);
  assert.equal(d.querySelectorAll('#plinesources img').length,0);
  assert.match(rows[0].textContent,/deals/);
  const silent=rows.find(row=>/SilentSource/.test(row.textContent));
  assert.match(silent.textContent,/watched/);
  assert.match(silent.textContent,/unaccounted/);
  assert.equal(silent.children[7].className,'bad');
  const quiet=rows.find(row=>/QuietSource/.test(row.textContent));
  assert.match(quiet.textContent,/silent/);
  assert.match(quiet.textContent,/nothing arrived/);
  // Latency is rendered as a duration, never as a raw float or "null".
  assert.match(rows[0].textContent,/40s/);
  assert.ok(!/null|undefined|NaN/.test(d.getElementById('plinesources').textContent));

  // Per-channel: configured-but-silent is called out.
  const targets=Array.from(d.querySelectorAll('#plinetargets tr'));
  assert.equal(targets.length,2);
  assert.match(targets[1].textContent,/Under99Loots/);
  assert.match(targets[1].textContent,/silent/);

  // Declared vs real is in words, not a JSON dump.
  assert.match(d.getElementById('plinedeclared').textContent,/4 source\(s\) in the saved config/);
  assert.match(d.getElementById('plinedeclared').textContent,/not in config: TrickXpert/);

  // Clicking the queue id fills the trace box and asks for that post.
  d.querySelector('#plineverdict [data-trace]').click();
  assert.equal(d.getElementById('traceid').value,'7');
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.ok(calls.some(c=>c.path.startsWith('/api/trace')),JSON.stringify(calls));

  // The export button asks for the window on screen, not a default.
  const opened=[];
  w.open=(url)=>{opened.push(url);return null;};
  d.querySelector('#pipeline button.ghost').click();
  assert.match(opened[0],/what=pipeline/);
  assert.match(opened[0],/hours=24/);
  assert.match(opened[0],/token=test/);

  // A clean window reads green and says so in words.
  payload={...payload,overall:'ok',unaccounted_ids:[],
    verdict:'Nothing dropped: 7 in = 6 posted + 1 withheld + 0 dropped (reason recorded) + 0 waiting + 0 failed',
    findings:[],tiles:payload.tiles.map(t=>t.key==='unaccounted'?{...t,value:0,state:'ok'}:t)};
  await w.loadPipeline();
  assert.equal(d.getElementById('plineverdict').className,'review-box okbox');
  assert.match(d.getElementById('plineverdict').textContent,/Nothing dropped/);
  assert.match(d.getElementById('plinefindings').textContent,/no findings/);

  // A dead endpoint cannot blank the panel or the rest of the page.
  const realFetch=w.fetch;
  w.fetch=async()=>{throw new Error('offline')};
  await w.loadPipeline();
  assert.match(d.getElementById('plinetiles').textContent,/pipeline view unavailable/);
  w.fetch=realFetch;

  console.log('Pipeline integrity DOM PASS: intake = posted + withheld + dropped + waiting + failed, a lost post turns the panel red with its queue id, per-source verdicts and reasons, configured-but-silent channels named, trace drill-down, durations never null, hostile text kept as text, dead endpoint contained.');
  dom.window.close();
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1});
