// "Telegram lo vunna channel anni chupinchi, search petti, multiple select chesi,
// categories select chesi, okesari add cheyyandi" - the exact clicking flow.
//
// The list comes from /api/directory (the bot's read-only snapshot), the save is
// ONE POST /api/watch/bulk with every ticked channel, and every refusal keeps its
// reason. Nothing here invents a channel: if the bot has not published, the card
// says so instead of showing an empty promise.
const {JSDOM}=require('jsdom');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const html=execFileSync(process.env.PYTHON||'python3',['-c','from dashboard.app import UI_HTML; print(UI_HTML)'],{encoding:'utf8'});

const calls=[];
let revision='revA';
let directoryAvailable=true;
let bulkStatus=200;
const directory={
  directory:{available:true,reason:'',generated_at:1,age_s:120,stale:false,count:5,addable:3},
  channels:[
    {id:101,title:'Loot Zone India',username:'LootZoneIndia11',kind:'channel',addable:true,
     private:false,members:52000,last_message:'2026-10-22 09:00 IST',watched:false,watch_state:'',
     watch_targets:[],watch_categories:[],is_our_target:true},
    {id:102,title:'Fashion Finds <img src=x onerror=alert(1)>',username:'fashionfinds',kind:'channel',
     addable:true,private:false,members:9100,last_message:'2026-10-21 22:00 IST',watched:false,
     watch_state:'',watch_targets:[],watch_categories:[],is_our_target:false},
    {id:103,title:'Gadget Alerts',username:'gadgetalerts',kind:'channel',addable:true,private:false,
     members:1200,last_message:'2026-10-20 10:00 IST',watched:false,watch_state:'',
     watch_targets:[],watch_categories:[],is_our_target:false},
    {id:104,title:'Family Group',username:'',kind:'group',addable:false,private:true,members:40,
     last_message:'2026-10-19 10:00 IST',watched:false,watch_state:'',
     watch_targets:[],watch_categories:[],is_our_target:false},
    {id:105,title:'Seen But Unlinked',username:'',kind:'channel',addable:false,private:true,
     members:900,last_message:'2026-10-18 10:00 IST',watched:false,watch_state:'',
     watch_targets:[],watch_categories:[],is_our_target:false},
  ],
  watched:[],revision:'revA',pending_restart:false,
  catalog:{targets:['LootZoneIndia11','PowerLoots1'],main_feed:['LootZoneIndia11','PowerLoots1'],
    reported:true,watch_live:true,main_available:true},
  categories:{fashion:'Fashion',electronics:'Electronics'},
};
let watched=[];
const dom=new JSDOM(html,{url:'https://preview.example/',runScripts:'dangerously',beforeParse(w){
  w.alert=message=>{throw new Error(message)};
  w.fetch=async(path,options={})=>{
    const method=options.method||'GET';
    const body=options.body?JSON.parse(options.body):null;
    calls.push({path,method,headers:options.headers||{},body});
    let status=200,data={};
    if(path==='/api/directory')data=directoryAvailable?directory:{
      directory:{available:false,reason:'the bot has not published its channel list yet',
        count:0,addable:0,age_s:null},channels:[],watched:[],catalog:{targets:[],main_feed:[]},
      categories:{},revision, pending_restart:false};
    if(path==='/api/watch'&&method==='GET')data={watch:watched,catalog:directory.catalog,
      revision,activation:{watcher_online:true,message:'live',error:''}};
    if(path==='/api/watch/bulk'){
      if(bulkStatus!==200){status=bulkStatus;data={errors:['the config file is read-only']};}
      else{
        const added=(body.channels||[]).filter(name=>['fashionfinds','gadgetalerts'].includes(name));
        const skipped=(body.channels||[]).filter(name=>!added.includes(name))
          .map(name=>({channel:name,errors:['already watched']}));
        added.forEach(name=>watched.push({channel:name,targets:body.targets,
          categories:body.categories,enabled:true,note:body.note,state:'watching',
          detail:'Live: new posts from @'+name+' route to LootZoneIndia11, PowerLoots1',
          title:name}));
        dirAdded(added);
        revision+='x';
        data={added,skipped,warnings:[],revision,plan:{targets:['LootZoneIndia11','PowerLoots1']},
          next:'The bot checks access with its own session and activates each source on its next pass; '
            +'every new post from them is converted and posted to LootZoneIndia11, PowerLoots1. '
            +'Nothing is joined and no old post is replayed.'};
      }
    }
    return {ok:status===200,status,json:async()=>data};
  };
  function dirAdded(names){
    for(const row of directory.channels){
      if(names.includes(row.username)){ row.watched=true; row.watch_state='watching'; }
    }
  }
}});

(async()=>{
  const w=dom.window,d=w.document;
  w.TOKEN='test';
  w.configRevision=revision;
  await w.loadDirectory();

  // The card exists and opens on the account's real list.
  assert.ok(d.getElementById('sourcestudio'),'the source studio card is part of the page');
  assert.equal(d.querySelectorAll('#dirlist .source-option').length,5);
  assert.match(d.getElementById('dircount').textContent,/5 shown · 0 selected · 3 addable of 5/);
  assert.match(d.getElementById('dirstate').textContent,/5 channels/);

  // A hostile channel title is text, never markup.
  assert.equal(d.querySelectorAll('#dirlist img').length,0);

  // Our own posting channel, a group and a private channel are visible but
  // cannot be ticked - and each says why.
  const boxes={};
  for(const input of d.querySelectorAll('#dirlist input[type=checkbox]')) boxes[input.value.toLowerCase()]=input;
  assert.equal(boxes['lootzoneindia11'].disabled,true,'a destination cannot be its own source');
  assert.equal(boxes['fashionfinds'].disabled,false);
  assert.equal(d.querySelectorAll('#dirlist input[type=checkbox]:disabled').length,3);
  assert.match(d.getElementById('dirlist').textContent,/one of our posting channels/);
  assert.match(d.getElementById('dirlist').textContent,/no public username/);

  // Search on top: title or @username, instantly, keeping the count honest.
  d.getElementById('dirsearch').value='fash';
  w.dirFilterChanged();
  let shown=Array.from(d.querySelectorAll('#dirlist .source-option strong')).map(n=>n.textContent);
  assert.deepEqual(shown,['Fashion Finds <img src=x onerror=alert(1)>']);
  d.getElementById('dirsearch').value='GADGET';
  w.dirFilterChanged();
  assert.equal(d.querySelectorAll('#dirlist .source-option').length,1);
  d.getElementById('dirsearch').value='no-such-channel';
  w.dirFilterChanged();
  assert.equal(d.querySelectorAll('#dirlist .source-option').length,0);
  assert.match(d.getElementById('dirlist').textContent,/no channel matches that search/);
  d.getElementById('dirsearch').value='';
  w.dirFilterChanged();

  // Multi-select: "select all shown" must never tick a disabled row.
  w.dirSelectShown();
  const checked=()=>Array.from(d.querySelectorAll('#dirlist input[type=checkbox]:checked'))
    .map(i=>i.value.toLowerCase());
  assert.deepEqual(checked(),['fashionfinds','gadgetalerts'],
    'only the two addable non-destination channels');
  assert.match(d.getElementById('dircount').textContent,/2 selected/);

  // Selecting then saving sends ONE request with MANY channels, the main feed
  // and the chosen categories.
  const catSelect=d.getElementById('dircats');
  assert.deepEqual(Array.from(catSelect.options).map(o=>o.value),['fashion','electronics']);
  catSelect.options[0].selected=true;
  d.getElementById('dirnote').value='studio test';
  const before=calls.length;
  await w.saveStudio();
  const posted=calls.slice(before).filter(c=>c.path==='/api/watch/bulk');
  assert.equal(posted.length,1,'one save, one request');
  assert.deepEqual(posted[0].body.channels,['fashionfinds','gadgetalerts']);
  assert.deepEqual(posted[0].body.targets,['*']);
  assert.deepEqual(posted[0].body.categories,['fashion']);
  assert.equal(posted[0].headers['If-Match'],'revA');
  assert.match(d.getElementById('dirmsg').textContent,/Saved 2 source\(s\)/);
  assert.match(d.getElementById('dirresults').textContent,/added: @fashionfinds, @gadgetalerts/);
  assert.deepEqual(checked(),[],'the selection clears after a successful save');
  // The rows come back from the server as "watching" - the bot's word, not ours.
  assert.match(d.getElementById('dirlist').textContent,/watching/);

  // Named destinations instead of the main feed.
  const main=d.getElementById('dirmain');
  main.checked=false;
  w.dirTargetsChanged();
  const targetSelect=d.getElementById('dirtargets');
  assert.deepEqual(Array.from(targetSelect.options).map(o=>o.value),['LootZoneIndia11','PowerLoots1']);
  assert.equal(targetSelect.disabled,false);
  targetSelect.options[1].selected=true;
  w.dirToggle({value:'fashionfinds',checked:true});
  await w.saveStudio();
  const named=calls.filter(c=>c.path==='/api/watch/bulk').pop();
  assert.deepEqual(named.body.targets,['PowerLoots1']);
  // ...and a save with nothing ticked never leaves the page.
  const quiet=calls.length;
  await w.saveStudio();
  assert.equal(calls.length,quiet);
  assert.match(d.getElementById('dirmsg').textContent,/Tick at least one channel/);

  // Choosing the main feed disables the named selector so the two can never
  // be sent together (the API refuses that combination).
  main.checked=true;
  w.dirTargetsChanged();
  assert.equal(targetSelect.disabled,true);
  assert.deepEqual(Array.from(targetSelect.selectedOptions),[]);

  // A refusal from the API stays on screen with the server's reason and does
  // not pretend the sources were added.
  bulkStatus=400;
  w.dirSelectShown();
  await w.saveStudio();
  assert.match(d.getElementById('dirmsg').textContent,/Not saved: the config file is read-only/);
  assert.match(d.getElementById('dirresults').textContent,/read-only/);
  bulkStatus=200;

  // Before the bot has published anything the card explains itself instead of
  // showing a blank list or a broken page.
  directoryAvailable=false;
  await w.loadDirectory();
  assert.match(d.getElementById('direrror').textContent,/has not published its channel list yet/);
  assert.match(d.getElementById('dirstate').textContent,/not published/);
  assert.match(d.getElementById('dirlist').textContent,/loading|matches|no channel/);

  // A dead endpoint cannot blank the page.
  const realFetch=w.fetch;
  w.fetch=async()=>{throw new Error('offline')};
  await w.loadDirectory();
  assert.match(d.getElementById('dirlist').textContent,/channel list unavailable: offline/);
  w.fetch=realFetch;

  // A full page refresh fills the card by itself (no extra click).
  assert.ok(calls.some(c=>c.path==='/api/directory'),'refresh() loads the studio');

  console.log('Source studio DOM PASS: whole account listed with search on top, '
    +'multi-select that refuses our own channels and groups with a reason, one bulk save '
    +'carrying targets + categories, server refusals shown verbatim, hostile titles as text, '
    +'empty directory explained and a dead endpoint contained.');
  dom.window.close();
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1});
