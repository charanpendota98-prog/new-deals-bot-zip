// Per-channel post buttons - the DOM workflow the operator actually clicks.
// "ah channel post inkoka channel buttons add cheyelaga": a post in one shelf
// offers the other shelves, in one press. No Telegram, no network, no real file.
const {JSDOM}=require('jsdom');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const html=execFileSync(process.env.PYTHON||'python3',['-c','from dashboard.app import UI_HTML; print(UI_HTML)'],{encoding:'utf8'});

const calls=[];
let view={
  buttons:[],
  per_channel:[],
  auto_label:'Join {title} ❤️',
  auto_limit:2,
  every:10,
  schedule:[true,false,false,false,false,false,false,false,false,false,true,false],
  max_buttons:2,
  max_label:40,
  path:'/bot/post_buttons.json',
  channels:[
    {name:'LootZoneIndia11', title:'Loot Zone India', url:'https://t.me/LootZoneIndia11',
     from:'dashboard', mode:'auto', where:'built from your other channels',
     every:10, every_own:false,
     schedule:[true,false,false,false,false,false,false,false,false,false,true,false],
     buttons:[{label:'Join Under 99 Shelf ❤️', url:'https://t.me/under99deals11'}]},
    {name:'under99deals11', title:'Under 99 Shelf', url:'https://t.me/under99deals11',
     from:'directory', mode:'custom', where:"this channel's own row",
     every:5, every_own:true,
     schedule:[true,false,false,false,false,true,false,false,false,false,true,false],
     buttons:[{label:'<img src=x onerror=alert(1)>', url:'https://t.me/LootZoneIndia11'}]},
    {name:'DailyStealsHub', title:'Daily Steals Hub', url:'https://t.me/DailyStealsHub',
     from:'dashboard', mode:'default', where:'the default row',
     every:10, every_own:false,
     schedule:[true,false,false,false,false,false,false,false,false,false,true,false],
     buttons:[{label:'Join Shopsy Loots ❤️', url:'https://t.me/shpsyloots'}]}
  ]
};

const dom=new JSDOM(html,{url:'https://preview.example/',runScripts:'dangerously',beforeParse(w){
  w.alert=message=>{throw new Error(message)};
  w.fetch=async(path,options={})=>{
    const method=options.method||'GET';
    const body=options.body?JSON.parse(options.body):null;
    calls.push({path,method,body});
    let status=200,data={};
    if(path==='/api/post-buttons'&&method==='GET')data=view;
    if(path==='/api/post-buttons'&&method==='PUT'){
      // Save exactly like the endpoint: only what the body carries changes.
      const next=JSON.parse(JSON.stringify(view));
      if(body.buttons) next.buttons=body.buttons;
      if(typeof body.every==='number') next.every=body.every;
      if(body.per_channel){
        for(const entry of body.per_channel){
          const row=next.channels.find(channel=>channel.name===entry.name);
          if(!row) continue;
          row.mode=entry.mode;
          if(typeof entry.every==='number' && entry.every>0){
            row.every=entry.every; row.every_own=true;
          }
          if(entry.mode==='custom') row.buttons=entry.buttons;
          if(entry.mode==='none') row.buttons=[];
          if(entry.mode==='auto') row.buttons=[{label:'Join Under 99 Shelf ❤️',
            url:'https://t.me/under99deals11'}];
          if(entry.mode==='default') row.buttons=next.buttons;
        }
        next.per_channel=body.per_channel;
      }
      data=next;
      view=next;
    }
    return {ok:status===200,status,json:async()=>data};
  };
}});

(async()=>{
  const w=dom.window,d=w.document;
  w.eval("TOKEN='test'");
  await w.loadButtons();

  // ---- the list --------------------------------------------------------- //
  const rows=d.querySelectorAll('#pbchannels .source-option');
  assert.equal(rows.length,3,'one row per channel we run');
  assert.match(d.getElementById('pbchannels').textContent,/built from your other channels/);
  assert.match(d.getElementById('pbchannels').textContent,/Join Under 99 Shelf/,
    'the resolved row is previewed, not just the mode');
  assert.match(d.getElementById('pbchannels').textContent,/the default row/);
  assert.equal(d.querySelectorAll('#pbchannels img').length,0,'a hostile label stays text');
  const modes=Array.from(d.querySelectorAll('#pbchannels select')).map(box=>box.value);
  assert.deepEqual(modes,['auto','custom','default']);
  assert.equal(d.getElementById('pbnote').textContent.includes('Join {title}'),true);
  assert.equal(d.querySelectorAll('#pbchannels a[href^="https://t.me/"]').length,3,
    'each channel is a real link');

  // ---- one channel's mode ------------------------------------------------ //
  await w.pbSetMode('LootZoneIndia11','auto');
  let put=calls.filter(call=>call.method==='PUT').pop();
  assert.deepEqual(put.body,{per_channel:[{name:'LootZoneIndia11',mode:'auto',buttons:[]}]},
    'only the channel that changed is sent');
  assert.match(d.getElementById('pballstate').textContent,/saved/);

  // ---- every channel at once --------------------------------------------- //
  await w.pbAutoAll();
  put=calls.filter(call=>call.method==='PUT').pop();
  assert.equal(put.body.per_channel.length,3);
  assert.equal(put.body.per_channel.every(entry=>entry.mode==='auto'),true);
  assert.match(d.getElementById('pballstate').textContent,/every channel now points at the others/);

  // ---- a custom row ------------------------------------------------------ //
  d.getElementById('pbchannels').innerHTML='';
  w.eval("pbConfig.channels[1].mode='custom'");
  w.renderMockButtons([]);
  w.pbRender();
  d.getElementById('pbc-under99deals11-0l').value='Join LootZone ❤️';
  d.getElementById('pbc-under99deals11-0u').value='https://t.me/LootZoneIndia11';
  await w.pbSaveCustom('under99deals11');
  put=calls.filter(call=>call.method==='PUT').pop();
  assert.deepEqual(put.body,{per_channel:[{name:'under99deals11',mode:'custom',
    buttons:[{label:'Join LootZone ❤️',url:'https://t.me/LootZoneIndia11'}]}]});

  // A half-filled row is refused in the page, before any request.
  const before=calls.length;
  d.getElementById('pbc-under99deals11-0l').value='Join LootZone';
  d.getElementById('pbc-under99deals11-0u').value='';
  await w.pbSaveCustom('under99deals11');
  assert.equal(calls.length,before,'a half-filled button is never sent');
  assert.match(d.getElementById('pballstate').textContent,/BOTH a label and a url/);

  const empty=calls.length;
  d.getElementById('pbc-under99deals11-0l').value='';
  await w.pbSaveCustom('under99deals11');
  assert.equal(calls.length,empty);
  assert.match(d.getElementById('pballstate').textContent,/at least one button/);

  // ---- how often --------------------------------------------------------- //
  assert.equal(d.getElementById('pbevery').value,'10','the global "every Nth" is loaded');
  assert.match(d.getElementById('pbschedule').textContent,/every|next 12/);
  assert.equal(d.getElementById('pbschedule').innerHTML.includes('●'),true,
    'the schedule is drawn as dots, not just a number');
  // every 5th, one press
  await w.pbPickEvery(5);
  let cadencePut=calls.filter(call=>call.method==='PUT').pop();
  assert.deepEqual(cadencePut.body,{every:5});
  assert.match(d.getElementById('pbeverystate').textContent,/every 5 post/);
  // a channel's own number - the mode it already has is carried, not reset
  const modeBefore=w.pbRows().find(row=>row.name==='DailyStealsHub').mode;
  await w.pbSetEvery('DailyStealsHub','7');
  cadencePut=calls.filter(call=>call.method==='PUT').pop();
  assert.equal(cadencePut.body.per_channel[0].name,'DailyStealsHub');
  assert.equal(cadencePut.body.per_channel[0].every,7);
  assert.equal(cadencePut.body.per_channel[0].mode,modeBefore,
    'the mode is not lost by a cadence edit');
  assert.match(d.getElementById('pballstate').textContent,/every 7 post/);
  // the per-channel boxes show what each channel is set to
  const everyBoxes=Array.from(d.querySelectorAll('#pbchannels input[type=number]'))
    .map(input=>input.value);
  assert.deepEqual(everyBoxes,['10','5','7']);
  assert.match(d.getElementById('pbchannels').textContent,/every 10 post/);
  assert.match(d.getElementById('pbchannels').textContent,/next 12:/);

  console.log('  PASS  dashboard DOM: per-channel button rows (modes, preview, links)');
  console.log('  PASS  dashboard DOM: how often (every 5th, every 10th, per channel, schedule)');
  console.log('  PASS  dashboard DOM: one channel, every channel, and a custom row');
  console.log('  PASS  dashboard DOM: a half-filled row never reaches the server');
  dom.window.close();
})().catch(error=>{ console.error(error); dom.window.close(); process.exit(1); });
