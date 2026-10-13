// Source-picker workflow tests with 60 feeds. No Telegram, network or real config writes.
const {JSDOM}=require('jsdom');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const html=execFileSync(process.env.PYTHON||'python3',['-c','from dashboard.app import UI_HTML; print(UI_HTML)'],{encoding:'utf8'});
const catalog=Array.from({length:60},(_,i)=>({key:'Feed'+String(i).padStart(3,'0'),title:'Source channel '+String(i).padStart(2,'0'),main:true}));
const rows=[],calls=[];let revision='r1',conflict=false;
const dom=new JSDOM(html,{url:'https://preview.example/',runScripts:'dangerously',beforeParse(w){
 w.confirm=()=>true;w.alert=message=>{throw new Error(message)};
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};
 w.fetch=async(path,options)=>{
  const body=options.body?JSON.parse(options.body):null;calls.push({path,...options,body});let data={},status=200;
  if(path==='/api/status')data={queue:{}};
  if(path==='/api/channels'&&options.method==='GET')data={revision,dashboard:rows,categories:{electronics:'Electronics'},source_catalog:catalog,sources:catalog.map(s=>s.key),source_limit:100};
  if(options.method==='POST'||options.method==='PATCH'){
   if(conflict||options.headers['If-Match']!==revision){status=409;data={errors:['Configuration changed; review again.']};}
   else{if(options.method==='POST')rows.push(body);else Object.assign(rows[0],body);revision+='x';data={channel:rows[0],warnings:[]};}
  }
  return {ok:status===200,status,json:async()=>data};
 };
}});
(async()=>{
 const w=dom.window,d=w.document;w.eval("TOKEN='test'");await w.refresh();
 assert.equal(d.querySelectorAll('#qsrcs input').length,60);
 assert.match(d.getElementById('qcount').textContent,/60 available/);
 d.getElementById('qsearch').value='channel 0';w.filterSources('q');w.selectVisibleSources('q');
 assert.equal(w.selectedSourceValues('q').length,10);
 d.getElementById('qsearch').value='no such channel';w.filterSources('q');
 assert.equal(d.getElementById('qempty').hidden,false);assert.equal(w.selectedSourceValues('q').length,10);
 d.getElementById('qlinks').value='https://t.me/ExtraDeals\n@ExtraDeals';w.addSourceLinks('q');
 assert.equal(w.selectedSourceValues('q').length,11);assert(w.selectedSourceValues('q').includes('ExtraDeals'));
 d.getElementById('qlinks').value='https://t.me/Feed000/123';w.addSourceLinks('q');
 assert.match(d.getElementById('qsourceerror').textContent,/message link/);assert.throws(()=>w.selectedSourceValues('q'),/message link/);
 d.getElementById('qlinks').value='';assert.equal(w.selectedSourceValues('q').length,11);
 await w.refresh();assert.equal(w.selectedSourceValues('q').length,11);
 // Selections belong to one channel form, not a global checkbox array.
 w.clearSources('f');const f=d.querySelector('#fsrcs input[value="Feed059"]');f.checked=true;f.dispatchEvent(new w.Event('change',{bubbles:true}));
 assert.deepEqual(Array.from(w.selectedSourceValues('f')),['Feed059']);assert.equal(w.selectedSourceValues('q').length,11);
 // Saving uses the actual selected feeds, not '*'.
 d.getElementById('quicklink').value='https://t.me/TechDestination';d.getElementById('quickcategory').value='electronics';
 await w.quickSave();const post=calls.find(c=>c.method==='POST');assert.equal(post.body.sources.length,11);assert(post.body.sources.includes('ExtraDeals'));assert(!post.body.sources.includes('*'));
 assert.deepEqual(post.body.categories,['electronics']);assert.equal(post.body.amazon_tag,false);
 // Edit retains all sources, supports a private invite, and uses revision protection.
 w.editShelf('TechDestination');assert.equal(w.selectedSourceValues('e').length,11);
 d.getElementById('elinks').value='https://telegram.me/joinchat/AlreadyJoinedHash';await w.saveShelf();
 const patch=calls.find(c=>c.method==='PATCH');assert.equal(patch.body.sources.length,12);assert(patch.body.sources.includes('https://t.me/+AlreadyJoinedHash'));
 // Poll updates names without losing selected checkboxes, keyboard focus or scroll position.
 d.getElementById('qsearch').value='';w.filterSources('q');d.querySelector('#qsrcs input[value="Feed000"]').focus();
 catalog[0].title='<img src=x onerror=alert(1)> renamed feed';await w.refresh();
 assert.equal(d.querySelectorAll('#qsrcs img').length,0);assert.equal(d.activeElement.value,'Feed000');assert.equal(w.selectedSourceValues('q').length,11);
 conflict=true;d.getElementById('quicklink').value='https://t.me/AnotherTarget';await w.quickSave();
 assert.match(d.getElementById('quickmsg').textContent,/Configuration changed/);assert.equal(d.getElementById('quicklink').value,'https://t.me/AnotherTarget');assert.equal(w.selectedSourceValues('q').length,11);
 w.clearSources('q');assert.throws(()=>w.selectedSourceValues('q'),/at least one/);
 console.log('Source picker DOM PASS: 60 named feeds, search, multi-select, hidden-selection retention, custom links, per-form isolation, exact POST/PATCH, conflicts, focus and safe text.');dom.window.close();
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1});
