// Optional jsdom test for the guided setup (mocked API, never live posting).
const { JSDOM } = require('jsdom');
const { execFileSync } = require('node:child_process');
const assert = require('node:assert/strict');
const html=execFileSync(process.env.PYTHON||'python3',['-c','from dashboard.app import UI_HTML; print(UI_HTML)'],{encoding:'utf8'});
const calls=[], rows=[];
let revision='r1', inspectionCategory='electronics', holdInspect=null, holdSave=null, conflict=false;
const dom=new JSDOM(html,{url:'https://preview.example',runScripts:'dangerously',beforeParse(w){
 w.confirm=()=>true; w.alert=msg=>{throw new Error(msg)};
 w.fetch=async(path,options)=>{
   const body=options.body?JSON.parse(options.body):null;
   calls.push({path,...options,body}); let data={},status=200;
   if(path==='/api/status') data={queue:{},db_exists:true};
   if(path==='/api/channels' && options.method==='GET') data={revision,dashboard:rows,categories:{fashion:'Clothing',electronics:'Electronics',home_kitchen:'Home & Kitchen'},sources:['deals'],category_rules_version:2};
   if(path==='/api/inspect-link'){
     if(holdInspect) await holdInspect;
     data={category:inspectionCategory,verdict:inspectionCategory?'suggested':'needs_details',evidence:[],matching_shelves:[],next_step:inspectionCategory?'Review this category.':'Paste the source product title.'};
   }
   if(path==='/api/channels' && options.method==='POST'){
     if(holdSave) await holdSave;
     if(conflict || options.headers['If-Match']!==revision){status=409;data={errors:['Refresh and review newer settings.']};}
     else { rows.push(body); revision='r2'; data={channel:body,warnings:[]}; }
   }
   return {ok:status===200,status,json:async()=>data};
 };
}});
(async()=>{
 const w=dom.window,d=w.document;
 w.eval("TOKEN='test'"); await w.refresh();
 assert.equal(d.querySelectorAll('#fcats input').length,3);
 w.setupNext(); assert.equal(d.getElementById('setupstep1').hidden,false);
 assert.match(d.getElementById('addmsg').textContent,/Choose at least one/);
 w.loadLinkExample(); await w.inspectLink();
 assert.equal(d.getElementById('icategory').textContent,'Electronics');
 assert.equal(d.getElementById('iuse').disabled,false);
 w.useInspection();
 assert.equal(d.getElementById('setupstep2').hidden,false);
 assert.equal(d.querySelector('#fcats input[value="electronics"]').checked,true);
 assert.equal(d.querySelector('#fcats input[value="fashion"]').checked,false);
 d.getElementById('fname').value='@TechDeals'; w.reviewSetup();
 assert.match(d.getElementById('addmsg').textContent,/Confirm that you created/);
 assert.equal(d.getElementById('setupstep3').hidden,true);
 d.getElementById('channelready').checked=true;d.getElementById('postingready').checked=true;
 w.reviewSetup(); assert.equal(d.getElementById('setupstep3').hidden,false);
 assert.match(d.getElementById('setupreview').textContent,/Categories: Electronics/);
 assert.match(d.getElementById('setupreview').textContent,/Amazon tag: Off/);
 let releaseSave;holdSave=new Promise(resolve=>releaseSave=resolve);
 const save=w.addChannel();await w.addChannel();
 assert.equal(d.getElementById('fname').disabled,true);
 assert.equal(calls.filter(c=>c.path==='/api/channels' && c.method==='POST').length,1);
 releaseSave();await save;holdSave=null;
 assert.equal(rows[0].name,'TechDeals');assert.deepEqual(rows[0].categories,['electronics']);
 assert.deepEqual(rows[0].sources,['*']);assert.equal(rows[0].amazon_tag,false);
 assert.match(d.getElementById('addmsg').textContent,/Activation pending/);
 assert.equal(calls.some(c=>c.path==='/api/restart'),false);
 assert.equal(d.getElementById('fname').disabled,false);
 // An opaque link cannot select a category and cannot reuse an earlier result.
 inspectionCategory=null;d.getElementById('ilink').value='https://amzn.to/opaque';
 d.getElementById('ilink').dispatchEvent(new w.Event('input'));await w.inspectLink();
 assert.equal(d.getElementById('iuse').disabled,true);
 assert.match(d.getElementById('icategory').textContent,/More product details/);
 // Late inspection response is ignored after the operator changes the input.
 let releaseInspect;holdInspect=new Promise(resolve=>releaseInspect=resolve);inspectionCategory='fashion';
 const pending=w.inspectLink();d.getElementById('ititle').value='New product';
 d.getElementById('ititle').dispatchEvent(new w.Event('input'));releaseInspect();await pending;holdInspect=null;
 assert.equal(d.getElementById('iuse').disabled,true);
 assert.match(d.getElementById('inext').textContent,/Inputs changed/);
 // Conflict preserves the form; a reviewed configuration is required to retry.
 d.getElementById('fname').value='SecondTech';d.getElementById('channelready').checked=true;d.getElementById('postingready').checked=true;
 w.reviewSetup();conflict=true;await w.addChannel();
 assert.equal(d.getElementById('fname').value,'SecondTech');assert.equal(rows.length,1);
 assert.match(d.getElementById('addmsg').textContent,/preserved/);
 // Quick setup accepts a public link with just a category, no wizard checklist.
 conflict=false;d.getElementById('quicklink').value='https://t.me/QuickClothes';
 d.getElementById('quickcategory').value='fashion';await w.quickSave();
 assert.equal(rows.length,2);assert.equal(rows[1].name,'QuickClothes');
 assert.deepEqual(rows[1].categories,['fashion']);assert.deepEqual(rows[1].sources,['*']);
 assert.equal(rows[1].amazon_tag,false);
 assert.match(d.getElementById('quickmsg').textContent,/Waiting for bot activation/);
 assert.equal(d.getElementById('quicklink').value,'');
 d.getElementById('quicklink').value='https://t.me/+PrivateInvite';
 const before=calls.filter(c=>c.method==='POST'&&c.path==='/api/channels').length;
 await w.quickSave();
 assert.equal(calls.filter(c=>c.method==='POST'&&c.path==='/api/channels').length,before);
 assert.match(d.getElementById('quickmsg').textContent,/not a private invite/);
 assert.equal(w.publicChannelName('t.me/QuickClothes'),'QuickClothes');
 console.log('Guided setup DOM PASS: cards, inspector, opaque/stale results, checklist, review, duplicate-save guard, safe defaults, conflict preservation.');
 w.close();
})().catch(e=>{console.error(e);process.exit(1)});
