// "Paste a channel link, its deals start posting" - the DOM workflow the
// operator actually clicks. No Telegram, no network, no real config writes.
const {JSDOM}=require('jsdom');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const html=execFileSync(process.env.PYTHON||'python3',['-c','from dashboard.app import UI_HTML; print(UI_HTML)'],{encoding:'utf8'});

const calls=[];
let revision='rev1';
let fail=false;
let watched=[{channel:'DealSourceX',targets:['*'],enabled:true,note:'',
  state:'watching',detail:'Live: new posts from @dealsourcex route to LootZoneIndia11, PowerLoots1',
  title:'<img src=x onerror=alert(1)> Deal Source'}];
const catalog={targets:['LootZoneIndia11','PowerLoots1','LootzoneTricks'],main_feed:['LootZoneIndia11','PowerLoots1'],
  reported:true,watch_live:true,main_available:true};
const dom=new JSDOM(html,{url:'https://preview.example/',runScripts:'dangerously',beforeParse(w){
  w.alert=message=>{throw new Error(message)};
  w.fetch=async(path,options={})=>{
    const method=options.method||'GET';
    const body=options.body?JSON.parse(options.body):null;
    calls.push({path,method,headers:options.headers||{},body});
    let status=200,data={};
    if(path==='/api/status')data={queue:{}};
    if(path==='/api/channels')data={revision,dashboard:[],categories:{},sources:[],source_catalog:[],source_limit:100};
    if(path==='/api/recent')data={jobs:[]};
    if(path==='/api/watch'&&method==='GET')data={watch:watched,catalog,revision,
      activation:{watcher_online:true,message:'live',error:''}};
    if(path==='/api/watch'&&method==='POST'){
      if(fail){status=400;data={errors:['that channel is not reachable'],warnings:[]};}
      else{
        if(options.headers['If-Match']&&options.headers['If-Match']!==revision){status=409;data={errors:['Configuration changed.']};}
        else{
          watched.push({channel:String(body.channel).replace(/^https:\/\/t\.me\//,''),targets:body.targets,
            enabled:true,note:body.note,state:'checking_access',detail:'checking access with the bot session'});
          revision+='x';data={source:body,warnings:['nothing is joined automatically'],
            next:'The bot will check access with its own session and start watching this channel; its next posts are converted and posted to LootZoneIndia11, PowerLoots1. Nothing is joined automatically and old posts are not replayed.'};
        }
      }
    }
    if(method==='PATCH'){watched[0].enabled=body.enabled;data={source:watched[0]};}
    if(method==='DELETE'){watched=watched.filter(row=>!path.endsWith(row.channel));data={removed:'x'};}
    return {ok:status===200,status,json:async()=>data};
  };
}});

(async()=>{
  const w=dom.window,d=w.document;
  w.eval("TOKEN='test'");
  await w.refresh();   // a real page load: the 5 s tick fills the card too

  // The bot's own verdict is what the row shows - never a promise of our own.
  const rows=d.querySelectorAll('#watchrows tr');
  assert.equal(rows.length,1);
  assert.match(rows[0].textContent,/DealSourceX/);
  assert.match(rows[0].textContent,/watching/);
  assert.match(rows[0].textContent,/route to LootZoneIndia11, PowerLoots1/);
  assert.equal(d.querySelectorAll('#watchrows img').length,0,'a hostile channel title stays text');
  assert.equal(d.getElementById('watchlive').textContent,'bot can watch live');
  assert.match(d.getElementById('watchfeednote').textContent,/Main feed = LootZoneIndia11, PowerLoots1/);
  const options=Array.from(d.getElementById('watchtargets').options).map(o=>o.value);
  assert.deepEqual(options,['LootZoneIndia11','PowerLoots1','LootzoneTricks']);

  // The main-feed default: one checkbox, no half-configured selection possible.
  assert.equal(d.getElementById('watchmain').checked,true);
  assert.equal(d.getElementById('watchtargets').disabled,true);

  // Paste a link, add it, and check the exact request the bot will read.
  d.getElementById('watchlink').value='https://t.me/FreshDealSource';
  d.getElementById('watchnote').value='from the WhatsApp group';
  w.paintWatchPlan();
  assert.match(d.getElementById('watchnext').textContent,/LootZoneIndia11, PowerLoots1/);
  await w.addWatch();
  let post=calls.filter(c=>c.method==='POST'&&c.path==='/api/watch').pop();
  assert.equal(post.body.channel,'https://t.me/FreshDealSource');
  assert.deepEqual(post.body.targets,['*']);
  assert.equal(post.body.note,'from the WhatsApp group');
  assert.equal(post.body.enabled,true);
  assert.equal(post.headers['If-Match'],'rev1','the stale-form guard is sent');
  assert.match(d.getElementById('watchmsg').textContent,/Saved\./);
  assert.match(d.getElementById('watchmsg').textContent,/Nothing is joined automatically/);
  assert.equal(d.getElementById('watchlink').value,'','the field clears for the next link');

  // Named destinations: unchecking the main feed enables the picker and is sent verbatim.
  d.getElementById('watchlink').value='NamedDestinationSource';
  d.getElementById('watchmain').checked=false;
  d.getElementById('watchmain').dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal(d.getElementById('watchtargets').disabled,false);
  d.getElementById('watchtargets').options[1].selected=true;
  d.getElementById('watchtargets').dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.match(d.getElementById('watchnext').textContent,/PowerLoots1/);
  await w.addWatch();
  post=calls.filter(c=>c.method==='POST'&&c.path==='/api/watch').pop();
  assert.deepEqual(post.body.targets,['PowerLoots1']);

  // A refused link says why and keeps what was typed (never a silent loss).
  fail=true;
  d.getElementById('watchlink').value='https://t.me/NotReachable';
  await w.addWatch();
  assert.match(d.getElementById('watchmsg').textContent,/Not saved: .*not reachable/);
  assert.equal(d.getElementById('watchlink').value,'https://t.me/NotReachable');
  fail=false;

  // A row without a link is refused before any request is made.
  const before=calls.length;
  d.getElementById('watchlink').value='   ';
  await w.addWatch();
  assert.equal(calls.length,before);
  assert.match(d.getElementById('watchmsg').textContent,/Paste the source channel link first/);

  // Per-row controls talk to the same API: pause, resume, remove.
  const toggle=d.querySelector('#watchrows [data-watch="toggle"]');
  await w.watchAction('toggle',toggle.dataset.channel,false);
  let patch=calls.filter(c=>c.method==='PATCH').pop();
  assert.equal(patch.path,'/api/watch/DealSourceX');
  assert.equal(patch.body.enabled,false);
  const remove=d.querySelector('#watchrows [data-watch="remove"]');
  await w.watchAction('remove',remove.dataset.channel,false);
  const del=calls.filter(c=>c.method==='DELETE').pop();
  assert.equal(del.path,'/api/watch/DealSourceX');

  // A dead endpoint cannot blank the section or the page.
  const realFetch=w.fetch;
  w.fetch=async()=>{throw new Error('offline')};
  await w.loadWatch();
  assert.match(d.getElementById('watchrows').textContent,/watch list unavailable/);
  w.fetch=realFetch;

  console.log('Watch-source DOM PASS: pasted link -> main feed or named destination, exact POST/PATCH/DELETE bodies, stale-form guard, live state and reason per row, safe text, refusals kept in the field, dead endpoint contained.');
  dom.window.close();
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1});
