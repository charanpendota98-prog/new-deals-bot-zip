// "Dashboard lo API key ichelaga vunchu, multiple ga" - the panel, clicked.
//
// The rule this suite exists for: a saved secret must never be readable from the
// page. Everything below asserts what an operator can SEE, and that adding a
// second key appends instead of replacing, that removing one drops exactly that
// one, and that a refusal from the server stays on screen with its reason.
const {JSDOM}=require('jsdom');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const html=execFileSync(process.env.PYTHON||'python3',['-c','from dashboard.app import UI_HTML; print(UI_HTML)'],{encoding:'utf8'});

const calls=[];
let revision='rev1';
let failNext=null;
let state={
  path:'/home/ubuntu/bestgaa-bot/bestgaa-bot/.env',
  app_dir:'/home/ubuntu/bestgaa-bot/bestgaa-bot',
  exists:true, writable:true, mode:'600', owner:'ubuntu',
  revision:'rev1', saved_ist:'2026-10-22 21:40 IST',
  bot_started_at:1792697046, bot_started_ist:'2026-10-22 19:24 IST', bot_has_it:true,
  groups:[{key:'telegram',label:'Telegram account'},{key:'earnkaro',label:'EarnKaro (commission)'},
          {key:'amazon',label:'Amazon Associates'},{key:'bitly',label:'Bitly (short links)'}],
  slots:[
    {key:'TELEGRAM_API_ID',label:'Telegram API ID',group:'telegram',help:'my.telegram.org',
     hint:'digits only',secret:false,multi:false,required:true,set:true,
     values:[{masked:'1234567',tail:''}],errors:[],warnings:[],info:['format looks right']},
    {key:'EARNKARO_API_KEY',label:'EarnKaro / Affiliaters token',group:'earnkaro',
     help:'Affiliaters → API page.',hint:'eyJ…',secret:true,multi:true,required:true,set:true,
     values:[{masked:'••••••4821',tail:'4821'}],errors:[],warnings:[],info:['pays publisher 5478322']},
    {key:'AMAZON_TAG',label:'Amazon Associates tag',group:'amazon',help:'Only a tag we own.',
     hint:'…-21',secret:false,multi:false,required:true,set:true,
     values:[{masked:'mama086-21',tail:''}],errors:[],warnings:[],info:['this is our tag']},
    {key:'BITLY_TOKENS',label:'Bitly access tokens',group:'bitly',help:'Add several.',
     hint:'20+ characters',secret:true,multi:true,required:false,set:true,
     values:[{masked:'••••••1111',tail:'1111'},{masked:'••••••2222',tail:'2222'}],
     errors:[],warnings:[],info:[]},
  ],
  leaks:{scanned:[{path:'/home/ubuntu/bestgaa-bot/bestgaa-bot/logs/bot.log',bytes:10}],hits:[],state:'clean'},
  journal:[{at:1792697000,action:'save',keys:[{key:'BITLY_TOKENS',action:'add',after:['••••••2222']}]}],
  readiness:{state:'attention',headline:'Working, with 1 thing(s) to look at: Bitly tokens.',
    checks:[{name:'EarnKaro token',state:'ok',detail:'1 value(s), format verified'},
            {name:'Bitly tokens',state:'warn',detail:'none'}],fail:0,warn:1},
  restart_command:'sudo systemctl restart bestgaa',
};

function nextState(patch){
  state={...state,...patch};
  if(state.slots)state.slots=state.slots.map(s=>({...s}));
}

const dom=new JSDOM(html,{url:'https://preview.example/',runScripts:'dangerously',beforeParse(w){
  w.alert=message=>{throw new Error(message)};
  w.confirm=()=>true;
  w.fetch=async(path,options={})=>{
    const method=options.method||'GET';
    const body=options.body?JSON.parse(options.body):null;
    calls.push({path,method,headers:options.headers||{},body});
    let status=200,data={};
    if(path==='/api/vault'&&method==='GET')data=state;
    if(path==='/api/vault'&&method==='PUT'){
      if(failNext){status=failNext.status;data=failNext.data;failNext=null;}
      else if((options.headers['If-Match']||revision)!==revision){
        status=409;data={errors:['.env changed since you opened this form.']};
      }else if(body.append&&body.append.AMAZON_TAG){
        status=400;data={errors:['AMAZON_TAG: someoneelse-21 is not a tag we own '
          +'(mama086-21) - using it would credit somebody else for our sales']};
      }else{
        const changes=[];
        if(body.append&&body.append.BITLY_TOKENS)changes.push({key:'BITLY_TOKENS',
          label:'Bitly access tokens',before:['••••••1111','••••••2222'],
          after:['••••••1111','••••••2222','••••••3333'],action:'add'});
        if(body.remove_values)changes.push({key:'BITLY_TOKENS',label:'Bitly access tokens',
          before:['••••••1111'],after:['••••••1111'],action:'replace'});
        const written=!body.dry_run;
        if(written){
          if(body.append&&body.append.BITLY_TOKENS){
            state.slots.find(s=>s.key==='BITLY_TOKENS').values.push({masked:'••••••3333',tail:'3333'});
          }
          if(body.remove_values){
            state.slots.find(s=>s.key==='BITLY_TOKENS').values=[{masked:'••••••1111',tail:'1111'}];
          }
          revision+='x'; state={...state,revision,bot_has_it:false};
        }
        data={dry_run:!!body.dry_run,written,changes,warnings:[],revision,
          bot_has_it:false,
          next:written?'Saved. The running bot reads .env at start, so restart it to apply - '
            +'or use the restart button below.':'Dry run only - nothing was written. Review the '
            +'changes, then save for real.'};
      }
    }
    return {ok:status===200,status,json:async()=>data};
  };
}});

(async()=>{
  const w=dom.window,d=w.document;
  w.TOKEN='test';
  await w.loadVault();

  // The card exists, is in the nav, and shows the verdict with its reasons.
  assert.ok(d.getElementById('vault'),'the API keys card is part of the page');
  assert.ok(d.querySelector('nav a[href="#vault"]'),'and reachable from the nav');
  assert.match(d.getElementById('vaultready').textContent,/Working, with 1 thing/);
  assert.equal(d.getElementById('vaultstate').textContent,'needs attention');
  assert.match(d.getElementById('vaultwhere').textContent,/the running bot has these keys/);
  assert.match(d.getElementById('vaultwhere').textContent,/ubuntu \/ 600/);

  // A saved secret is never rendered - only the masked tail.
  const page=d.getElementById('vaultslots').textContent;
  assert.match(page,/••••••4821/);
  assert.match(page,/••••••1111/);
  assert.match(page,/mama086-21/,'a non-secret slot is shown plainly');
  assert.ok(!/eyJ/.test(page),'no token body anywhere in the panel');
  // Required / several-allowed / set badges are visible per key.
  assert.equal(d.querySelectorAll('#vaultslots .pill.fail').length>0,true,'required is marked');
  assert.match(d.getElementById('vaultslots').textContent,/several allowed/);

  // Add a SECOND Bitly token: it stages locally, typed into a password field.
  const input=d.getElementById('vaultin-BITLY_TOKENS');
  assert.equal(input.type,'password','the input does not echo a key on screen');
  input.value='bitlytokenthree0123456789';
  w.vaultAdd('BITLY_TOKENS');
  assert.match(d.getElementById('vaultslots').textContent,/new …6789/);
  assert.equal(input.value==='',true,'the field clears after staging');
  assert.equal(calls.filter(c=>c.method==='PUT').length,0,'staging writes nothing');

  // A key with whitespace is refused locally (a wrapped paste is the usual cause).
  const before=calls.length;
  d.getElementById('vaultin-EARNKARO_API_KEY').value='eyJ abc';
  w.vaultAdd('EARNKARO_API_KEY');
  assert.equal(calls.length,before);
  assert.match(d.getElementById('vaultmsg').textContent,/cannot contain spaces/);

  // Preview = dry run: the server is asked, and nothing is written.
  await w.previewVault();
  let put=calls.filter(c=>c.method==='PUT').pop();
  assert.equal(put.body.dry_run,true);
  assert.deepEqual(put.body.append,{BITLY_TOKENS:['bitlytokenthree0123456789']});
  assert.match(d.getElementById('vaultmsg').textContent,/Dry run only/);
  assert.match(d.getElementById('vaultchanges').textContent,/Would change/);
  assert.match(d.getElementById('vaultchanges').textContent,/••••••3333/);

  // Save for real: append (not replace), masked answer, draft cleared, panel reloaded.
  await w.saveVault(false);
  put=calls.filter(c=>c.method==='PUT').pop();
  assert.equal(put.body.dry_run,false);
  assert.equal(put.headers['If-Match'],'rev1','the form is guarded by the file revision');
  assert.match(d.getElementById('vaultmsg').textContent,/Saved\./);
  assert.match(d.getElementById('vaultmsg').textContent,/restart/i);
  assert.equal(d.getElementById('vaultin-BITLY_TOKENS').value,'');
  assert.equal(d.getElementById('vaultslots').textContent.includes('new …6789'),false,
    'the staged chip becomes a stored value');
  assert.match(d.getElementById('vaultslots').textContent,/••••••3333/);
  assert.equal(d.querySelectorAll('#vaultslots').length,1);

  // Remove exactly ONE stored value by position.
  const chips=[...d.querySelectorAll('#vaultslots .key-chip')];
  const secondBitly=chips.filter(c=>/••••/.test(c.textContent)).pop();
  assert.ok(secondBitly,'the stored chips are removable');
  const removeButton=secondBitly.querySelector('.chip-x');
  removeButton.dispatchEvent(new w.Event('click',{bubbles:true}));
  await w.saveVault(false);
  put=calls.filter(c=>c.method==='PUT').pop();
  assert.ok(put.body.remove_values && put.body.remove_values.BITLY_TOKENS,'removal is by index');
  assert.match(d.getElementById('vaultmsg').textContent,/Saved\./);

  // A refusal from the server stays on screen with its reason and changes nothing.
  d.getElementById('vaultin-AMAZON_TAG').value='someoneelse-21';
  w.vaultAdd('AMAZON_TAG');
  await w.saveVault(false);
  assert.match(d.getElementById('vaultmsg').textContent,/Not saved: AMAZON_TAG/);
  assert.match(d.getElementById('vaultchanges').textContent,/is not a tag we own/);
  assert.match(d.getElementById('vaultchanges').textContent,/credit somebody else/);

  // A refused save keeps the staged value (the operator must not retype a good
  // key because one other key was bad) - drop it, then nothing is staged.
  w.vaultDropNew('AMAZON_TAG',0);
  const quiet=calls.length;
  await w.saveVault(false);
  assert.equal(calls.length,quiet);
  assert.match(d.getElementById('vaultmsg').textContent,/Nothing to save/);

  // A stale form (another tab saved first) is reported, not retried blindly.
  failNext={status:409,data:{errors:['.env changed since you opened this form.']}};
  d.getElementById('vaultin-BITLY_TOKENS').value='bitlytokenfour0123456789';
  w.vaultAdd('BITLY_TOKENS');
  await w.saveVault(false);
  assert.match(d.getElementById('vaultmsg').textContent,/Not saved: .env changed/);

  // Leak scan and journal are rendered (counts and masked tails only).
  assert.match(d.getElementById('vaultleaks').textContent,/No stored key found/);
  assert.match(d.getElementById('vaultjournal').textContent,/BITLY_TOKENS add/);
  assert.ok(!/bitlytoken/i.test(d.getElementById('vaultjournal').textContent),
    'the journal never shows a clear key');

  // A dead endpoint cannot blank the page or throw.
  const realFetch=w.fetch;
  w.fetch=async()=>{throw new Error('offline')};
  await w.loadVault();
  assert.match(d.getElementById('vaultmsg').textContent,/API keys unavailable: offline/);
  w.fetch=realFetch;

  // A hostile stored label stays text.
  state.slots[0].label='<img src=x onerror=alert(1)>';
  await w.loadVault();
  assert.equal(d.querySelectorAll('#vaultslots img').length,0,'labels are escaped');

  console.log('API keys DOM PASS: verdict + reasons, masked values only, staging a second key '
    +'in a password field, local whitespace refusal, dry run then save with If-Match, '
    +'remove-by-index, server refusal verbatim, no-op sends nothing, leak scan and masked journal.');
  dom.window.close();
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1});
