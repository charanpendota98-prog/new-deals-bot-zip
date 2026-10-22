// "Is this post ours, and did anything get lost?" - the dossier panel, clicked.
//
// Everything is mocked: the assertions are about what an operator can see and
// trust. A link that is not proven ours must LOOK not-ours, a refusal must show
// its reason, and a missing queue row must say so instead of drawing a blank.
const {JSDOM}=require('jsdom');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const html=execFileSync(process.env.PYTHON||'python3',['-c','from dashboard.app import UI_HTML; print(UI_HTML)'],{encoding:'utf8'});

const calls=[];
let mode='clean';
const clean={
  generated_at:1, post:{queue_id:41,chat_id:-1,msg_id:7,source:'fashionfinds',status:'done',
    created_at:1,created_ist:'2026-10-22 21:10 IST',age:'12m',attempts:1,error:'',
    quality:80,premium_score:3,rendered:'Blanket ₹100\nhttps://www.amazon.in/dp/X?tag=mama086-21',
    source_text:'Blanket ₹1,999 → ₹100\nhttps://www.amazon.in/dp/X',
    targets:['LootZoneIndia11','PowerLoots1']},
  words:{source:12,rendered:14},
  states:{state:'delivered',received:['LootZoneIndia11','PowerLoots1'],withheld:[],failed:[],pending:[]},
  verdict:'posted to 2 channel(s) · every link proven ours',
  links:{published:[{url:'https://www.amazon.in/dp/X?tag=mama086-21',host:'www.amazon.in',ours:true,
      kind:'amazon_tag',proof:'our tag mama086-21',resolved:'',from_source:false,risk:''}],
    source:[{url:'https://www.amazon.in/dp/X',host:'www.amazon.in',
      published_as:'https://www.amazon.in/dp/X?tag=mama086-21',kept:false,rewritten:true,
      dropped:false,cached:true}],
    proven:1,unproven:0,our_tag:'mama086-21'},
  diff:{removed:['Blanket ₹1,999 → ₹100'],added:['Blanket ₹100'],removed_count:1,added_count:1,identical:false},
  numbers:{source:['1999','100'],rendered:['100'],lost:[]},
  timeline:[{at:1,at_ist:'2026-10-22 21:10 IST',kind:'intake',target:'',detail:'taken in from fashionfinds'},
            {at:2,at_ist:'2026-10-22 21:10 IST',kind:'delivery',target:'LootZoneIndia11',detail:'sent'}],
  findings:[],latency_seconds:4,deliveries:[],blocks:[],policy:{snapshot:true,channels:true},
};
const dirty=JSON.parse(JSON.stringify(clean));
dirty.verdict='posted to 1 channel(s) · 1 link(s) NOT proven ours';
dirty.links.published=[{url:'https://www.amazon.in/dp/Y',host:'www.amazon.in',ours:false,
  kind:'foreign_tag',proof:'',resolved:'',from_source:true,
  risk:'tag=someoneelse-21 is not ours (mama086-21) - this pays someone else'}];
dirty.links.unproven=1;dirty.links.proven=0;
dirty.findings=[
  {severity:'error',code:'unproven_link',detail:'1 published link(s) are not proven ours: www.amazon.in',
   fix:'run ops/link_doctor.py on the source post'},
  {severity:'warn',code:'zero_chunks',detail:'recorded as delivered with 0 chunks: PowerLoots1',
   fix:'a zero-byte send is a silent miss; verify the channel by hand'}];
dirty.post.source_text='<img src=x onerror=alert(1)> deal';
dirty.states={state:'withheld',received:[],withheld:['LootZoneIndia11'],failed:[],pending:[]};
dirty.timeline=[{at:1,at_ist:'2026-10-22 21:10 IST',kind:'refused',target:'LootZoneIndia11',
  detail:'presend: Above price cap - This shelf only takes deals under the cap.'}];
dirty.blocks=[{at:1,at_ist:'2026-10-22 21:10 IST',stage:'presend',target:'LootZoneIndia11',
  category:'price_cap',label:'Above price cap',matched:'880',reason:'above the cap',excerpt:'****',policy_version:'v1'}];

const dom=new JSDOM(html,{url:'https://preview.example/',runScripts:'dangerously',beforeParse(w){
  w.alert=message=>{throw new Error(message)};
  w.fetch=async(path,options={})=>{
    calls.push({path,method:options.method||'GET'});
    let status=200,data={};
    if(path.startsWith('/api/forensics')){
      if(path.includes('queue_id=404')){status=404;data={error:'no queue row 404'};}
      else if(path.includes('newest'))data=dirty;
      else data=(path.includes('queue_id=42')||mode==='dirty')?dirty:clean;
    }
    return {ok:status===200,status,json:async()=>data};
  };
}});

(async()=>{
  const w=dom.window,d=w.document;
  w.TOKEN='test';
  assert.ok(d.getElementById('dossier'),'the dossier card is part of the page');
  assert.ok(d.querySelector('nav a[href="#dossier"]'),'and it is reachable from the nav');

  // A clean post reads as clean, and every claim has its evidence.
  await w.openDossier(41);
  assert.match(d.getElementById('dossierhead').textContent,/#41/);
  assert.match(d.getElementById('dossierverdict').textContent,/every link proven ours/);
  assert.equal(d.querySelectorAll('#dossierfindings .pill.fail').length,0);
  assert.match(d.getElementById('dossierfindings').textContent,/No findings/);
  const facts=d.getElementById('dossierfacts').textContent;
  assert.match(facts,/proven ours/);
  assert.match(d.getElementById('dossierlinks').textContent,/amazon_tag/);
  assert.match(d.getElementById('dossierlinks').textContent,/our tag mama086-21/);
  assert.match(d.getElementById('dossiersource').textContent,/rewritten to ours/);
  assert.match(d.getElementById('dossiertimeline').textContent,/intake/);
  assert.match(d.getElementById('dossierdiff').textContent,/line\(s\) dropped/);
  assert.match(d.getElementById('dossiertext').textContent,/Published text/);

  // The expensive failure: an unproven, foreign-tagged link must LOOK wrong.
  mode='dirty';
  await w.openDossier(42);
  assert.match(d.getElementById('dossierverdict').textContent,/NOT proven ours/);
  const findings=d.getElementById('dossierfindings');
  assert.equal(findings.querySelectorAll('.pill.fail').length,1,'the error is red');
  assert.equal(findings.querySelectorAll('.pill.warn').length,1,'the warning is amber');
  assert.match(findings.textContent,/unproven_link/);
  assert.match(findings.textContent,/link_doctor/);
  assert.match(d.getElementById('dossierlinks').textContent,/foreign_tag/);
  assert.match(d.getElementById('dossierlinks').textContent, /this pays someone else/);
  assert.match(d.getElementById('dossierlinks').textContent,/copied from the source post/);
  assert.match(d.getElementById('dossiertimeline').textContent,/refused/);
  assert.match(d.getElementById('dossiertimeline').textContent,/Above price cap/);
  // A hostile stored text stays text: no element, no execution.
  assert.equal(d.querySelectorAll('#dossiertext img').length,0,'stored text is escaped');
  assert.match(d.getElementById('dossiertext').textContent,/<img src=x/);

  // "Newest refused post" asks the server for the hole, not for a number.
  await w.openDossier('newest');
  assert.match(calls[calls.length-1].path,/queue_id=newest/);

  // A non-numeric id never leaves the browser.
  const before=calls.length;
  await w.openDossier('abc');
  assert.equal(calls.length,before);
  assert.match(d.getElementById('dossiererror').textContent,/numeric queue id/);

  // A missing row is an honest 404, not a blank dossier.
  await w.openDossier(404);
  assert.match(d.getElementById('dossiererror').textContent,/no queue row 404/);

  // A dead endpoint cannot break the page.
  const realFetch=w.fetch;
  w.fetch=async()=>{throw new Error('offline')};
  await w.openDossier(41);
  assert.match(d.getElementById('dossiererror').textContent,/dossier failed: offline/);
  w.fetch=realFetch;

  // Copy works (or falls back to showing the JSON) without throwing.
  await w.openDossier(41);
  w.copyDossier();
  assert.match(d.getElementById('dossiererror').textContent,/copied|"post"|queue_id/);

  console.log('Deal dossier DOM PASS: verdict + evidence per link, unproven links flagged '
    +'red with the reason, refusals on the timeline, stored text escaped, newest-hole button, '
    +'id validation, honest 404 and a dead endpoint contained.');
  dom.window.close();
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1});
