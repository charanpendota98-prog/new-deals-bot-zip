// "Attach what already runs, and get told what to add next" - the DOM workflow.
// No Telegram, no network, no real config writes: fetch is mocked and the page's
// own functions are called exactly as a click would.
const {JSDOM}=require('jsdom');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const html=execFileSync(process.env.PYTHON||'python3',['-c','from dashboard.app import UI_HTML; print(UI_HTML)'],{encoding:'utf8'});

const calls=[];
let revision='rev1';

function row(over){
  return Object.assign({name:'', link:null, origins:[], ours:false, attached:false, paused:false,
    role:'creator', admin:true, creator:true, can_post:true, kind:'channel', members:0,
    attachable:false, detachable:false, why_not:'', note:'', mode:'', seen_by_bot:true}, over);
}
let view={
  rows:[
    row({name:'LootZoneIndia11', link:'https://t.me/LootZoneIndia11', origins:['code'], ours:true,
      attachable:true, members:52000}),
    row({name:'NewshelfDeals', link:'https://t.me/NewshelfDeals', origins:[], attachable:true,
      members:12}),
    row({name:'Under99Deals11', link:'https://t.me/Under99Deals11', origins:['code','attached'],
      ours:true, attached:true, detachable:true, note:'the busy shelf'}),
    row({name:'<img src=x onerror=alert(1)>', link:null, origins:[], attachable:false,
      why_not:'you are only a member there'}),
  ],
  counts:{ours:2, attached:1, paused:0, candidates:2, code:2, dashboard:0},
  revision, directory_available:true, directory_age_s:12,
  suggestions:[], suggest_queue:[], paths:{requests:'/x/suggest_requests.jsonl'},
  note:'attaching never posts'
};

const answerFor=channel=>({id:'abc', channel, link:'https://t.me/'+channel, at:1700000000,
  count:2, similar_count:1, mentioned_count:1, error:'', notes:[],
  suggestions:[
    {username:'SimilarDeals', title:'Similar Deals', link:'https://t.me/SimilarDeals',
     members:1200, source:'similar', why:'Telegram recommends it next to @'+channel},
    {username:'MentionedDeal', title:'Mentioned Deal', link:'https://t.me/MentionedDeal',
     members:0, source:'mentioned', why:'linked 3x in the recent posts of @'+channel}
  ]});

const dom=new JSDOM(html,{url:'https://preview.example/',runScripts:'dangerously',beforeParse(w){
  w.alert=message=>{throw new Error(message)};
  w.fetch=async(path,options={})=>{
    const method=options.method||'GET';
    const body=options.body?JSON.parse(options.body):null;
    calls.push({path,method,headers:options.headers||{},body});
    let status=200,data={};
    if(path==='/api/own'&&method==='GET')data=view;
    if(path==='/api/own/attach'){
      data={attached:{name:body.name, mode:'new', posting:true},
        warnings:['@'+body.name+' is not a target yet: attaching adds it as a dashboard channel'],
        next:'@'+body.name+' is attached as a dashboard channel. The bot starts posting there once it picks the config up.',
        view};
    }
    if(path==='/api/own/posting'){
      data={row:{name:body.name, posting:body.posting},
        next:body.posting ? '@'+body.name+' is posting again from the next post on. Posts that arrived while it was paused are not replayed.'
                          : '@'+body.name+' is paused: it receives nothing new, and no delivery row is written for it. Resuming applies to future posts - nothing is replayed.',
        view};
    }
    if(path==='/api/own/suggest'&&method==='POST'){
      data={queued:{id:'abc', channel:body.channel},
        next:'The bot will look at @'+body.channel+' within a few seconds and write what it finds here.'};
    }
    if(path==='/api/own/suggest'&&method==='GET'){
      data={suggestions:[answerFor('LootZoneIndia11')], queued:[], paths:{}};
    }
    if(path==='/api/watch')data={source:{channel:body.channel}, next:'nothing is joined'};
    return {ok:status===200,status,json:async()=>data};
  };
}});

(async()=>{
  const w=dom.window,d=w.document;
  w.eval("TOKEN='test'");

  // ---- one list ---------------------------------------------------------- //
  await w.loadOwn();
  assert.equal(d.querySelectorAll('#ownlist .source-option').length,4,'every row is rendered');
  assert.match(d.getElementById('ownsummary').textContent,/2 channel\(s\) ours/);
  assert.match(d.getElementById('owncount').textContent,/2 ours/);
  assert.equal(d.getElementById('ownstate').textContent,'1 attached');
  assert.equal(d.querySelectorAll('#ownlist a[href^="https://t.me/"]').length,3,
    'a channel with a username is a real link');
  assert.match(d.getElementById('ownlist').textContent,/you are only a member there/,
    'a channel we cannot attach says why');
  assert.equal(d.querySelectorAll('#ownlist img').length,0,'a hostile title stays text');
  // Attach is offered for the two we can attach, Pause/Detach/Suggest for ours.
  assert.equal(d.querySelectorAll('#ownlist button').length>=6,true);
  assert.match(d.getElementById('ownlist').innerHTML,/ownAttach\('NewshelfDeals'\)/);
  assert.match(d.getElementById('ownlist').innerHTML,/ownPosting\('Under99Deals11',false\)/);
  assert.match(d.getElementById('ownlist').innerHTML,/ownDetach\('Under99Deals11'\)/);
  assert.match(d.getElementById('ownlist').innerHTML,/ownSuggest\('LootZoneIndia11'\)/);

  // ---- attach a paste-link ----------------------------------------------- //
  d.getElementById('owninput').value='https://t.me/NewshelfDeals';
  await w.ownAttachInput();
  const attach=calls.filter(c=>c.method==='POST'&&c.path==='/api/own/attach').pop();
  assert.match(attach.body.name,/NewshelfDeals$/,
    'the pasted link reaches the server, which is the authority on the handle');
  assert.equal(attach.headers['If-Match'],'rev1','the stale-form guard is sent');
  assert.match(d.getElementById('ownmsg').textContent,/picks the config up/);
  assert.match(d.getElementById('ownmsg').textContent,/not a target yet/,
    'the warning is shown, not swallowed');
  assert.equal(d.getElementById('owninput').value,'','the box is cleared after a save');

  // ---- pause ------------------------------------------------------------- //
  await w.ownPosting('Under99Deals11',false);
  const pause=calls.filter(c=>c.path==='/api/own/posting').pop();
  assert.deepEqual(pause.body,{name:'Under99Deals11',posting:false});
  assert.match(d.getElementById('ownmsg').textContent,/nothing is replayed/);

  // ---- suggest ----------------------------------------------------------- //
  await w.ownSuggest('LootZoneIndia11');
  const ask=calls.filter(c=>c.method==='POST'&&c.path==='/api/own/suggest').pop();
  assert.deepEqual(ask.body,{channel:'LootZoneIndia11'});
  assert.match(d.getElementById('sgmsg').textContent,/will look at @LootZoneIndia11/);
  // The bot's answer is what the panel shows, with its provenance. (`sgAnswers`
  // is a top-level `let` in the page script - reachable from a global eval, not
  // from a property on window - so the fixture is injected the way the page sees it.)
  w.eval("sgAnswers = " + JSON.stringify([answerFor('LootZoneIndia11')]));
  w.renderSuggestions();
  assert.equal(d.querySelectorAll('#sglist .source-option').length,2);
  assert.match(d.getElementById('sglist').textContent,/Telegram recommends it next to @LootZoneIndia11/);
  assert.match(d.getElementById('sglist').textContent,/linked 3x/);
  assert.match(d.getElementById('sglist').innerHTML,/ownAddSource\('SimilarDeals'\)/);
  assert.match(d.getElementById('sglist').innerHTML,/ownAttach\('SimilarDeals'\)/);

  // ---- add a suggestion as a source -------------------------------------- //
  await w.ownAddSource('SimilarDeals');
  const source=calls.filter(c=>c.method==='POST'&&c.path==='/api/watch').pop();
  assert.equal(source.body.channel,'SimilarDeals');
  assert.deepEqual(source.body.targets,['*']);

  // ---- an unavailable directory is a sentence, not a crash ---------------- //
  view=Object.assign({},view,{rows:[],directory_available:false});
  d.getElementById('ownmsg').textContent='';   // a fresh page, no action message yet
  await w.loadOwn();
  assert.match(d.getElementById('ownmsg').textContent,/has not published its channel list yet/);

  console.log('  PASS  dashboard DOM: our channels (attach, pause, list, hostile text)');
  console.log('  PASS  dashboard DOM: suggestions (provenance, add as source, attach)');
  console.log('  PASS  dashboard DOM: no directory still renders an explanation');
  dom.window.close();
})().catch(error=>{ console.error(error); dom.window.close(); process.exit(1); });
