// Optional DOM workflow tests. See README for isolated jsdom installation.
// The API is mocked; no real bot, server, network, or channel writes occur.
const { JSDOM } = require('jsdom');
const { execFileSync } = require('node:child_process');
const assert = require('node:assert/strict');
const html = execFileSync(process.env.PYTHON || 'python3', ['-c', 'from dashboard.app import UI_HTML; print(UI_HTML)'], {encoding:'utf8'});
const calls=[];
let revision='r1', conflict=false, failBatch=false, holdBatch=null;
const rows=[{name:'ClothingShelf',sources:['*'],categories:['fashion'],enabled:true}];
const report={summary:{total:2,matched:1,withheld:1,evaluated:2,expectation_mismatches:1},notice:'No sends. Saved config only.',pending_restart:true,
  results:[{text:'<img src=x onerror=alert(1)> Cotton kurti',category:'fashion',reason:'matched',expected_category:'fashion',expectation_match:true,
    channels:[{channel:'ClothingShelf',state:'candidate',reason:'category_and_source_match'}]},
    {text:'Shoes and shoe rack',category:null,reason:'mixed_or_conflicting',expected_category:'fashion',expectation_match:false,
      channels:[{channel:'ClothingShelf',state:'blocked',reason:'category_withheld'}]}]};
const dom=new JSDOM(html,{url:'https://preview.example/',runScripts:'dangerously',beforeParse(w){
  w.confirm=()=>true; w.alert=msg=>{throw new Error(msg)};
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
  w.HTMLDialogElement.prototype.close=function(){this.open=false};
  w.fetch=async(path,opts)=>{
    const body=opts.body?JSON.parse(opts.body):null; calls.push({path,...opts,body});
    let data={},status=200;
    if(path==='/api/status') data={queue:{},db_exists:true};
    if(path==='/api/channels') data={revision,dashboard:rows,categories:{fashion:'Clothing',electronics:'Electronics'},sources:['deals'],category_rules_version:2};
    if(opts.method==='PATCH'){
      if(conflict || opts.headers['If-Match']!==revision){status=409; data={errors:['Refresh and review newer settings.']};}
      else {rows[0].categories=body.categories; revision='r2';}
    }
    if(path==='/api/category-batch'){
      if(holdBatch) await holdBatch;
      if(failBatch){status=400;data={errors:['invalid batch']};} else data=report;
    }
    if(path==='/api/category-preview') data={category:'fashion',reason:'matched',rules_version:2,evidence:[],channels:report.results[0].channels,notice:'No sends.'};
    return {ok:status===200,status,json:async()=>data};
  };
}});
(async()=>{
 const w=dom.window,d=w.document; w.eval("TOKEN='test'"); await w.refresh();
 w.editShelf('ClothingShelf');
 assert.equal(d.getElementById('shelfeditor').open,true);
 assert.equal(d.querySelector('#editcats input[value="fashion"]').checked,true);
 d.querySelector('#editcats input[value="fashion"]').checked=false;
 await w.saveShelf();
 assert.match(d.getElementById('editerror').textContent,/Select at least one/);
 assert.equal(calls.some(c=>c.method==='PATCH'),false);
 d.querySelector('#editcats input[value="electronics"]').checked=true;
 await w.saveShelf();
 assert.deepEqual(rows[0].categories,['electronics']);
 assert.equal(d.getElementById('shelfeditor').open,false);
 assert.equal(calls.find(c=>c.method==='PATCH').headers['If-Match'],'r1');
 // A rejected stale form stays open and does not overwrite newer state.
 w.editShelf('ClothingShelf'); conflict=true;
 await w.saveShelf();
 assert.equal(d.getElementById('shelfeditor').open,true);
 assert.match(d.getElementById('editerror').textContent,/Refresh/);
 assert.deepEqual(rows[0].categories,['electronics']);
 d.getElementById('editmode').value='all'; w.toggleEditMode(); w.confirm=()=>false;
 const patches=calls.filter(c=>c.method==='PATCH').length; await w.saveShelf();
 assert.equal(calls.filter(c=>c.method==='PATCH').length,patches);
 d.getElementById('btext').value='Cotton kurti\n---\nShoes and shoe rack'; d.getElementById('bsource').value='deals';
 await w.runBatch();
 const batch=calls.find(c=>c.path==='/api/category-batch');
 assert.equal(batch.body.items.length,2); assert.equal(batch.body.items[0].source,'deals');
 assert.equal(d.querySelectorAll('#batchresults tr').length,2);
 assert.equal(d.querySelectorAll('#batchresults img').length,0); // no injection from pasted captions
 assert.match(d.getElementById('bsummary').textContent,/2 tested/);
 assert.match(d.getElementById('bnotice').textContent,/waiting for restart/);
 assert.equal(d.getElementById('bexport').disabled,false);
 d.getElementById('bfilter').value='withheld'; w.renderBatch(); assert.equal(d.querySelectorAll('#batchresults tr').length,1);
 d.getElementById('bfilter').value='mismatch'; w.renderBatch(); assert.equal(d.querySelectorAll('#batchresults tr').length,1);
 d.getElementById('bformat').value='json'; d.getElementById('btext').value='not json'; await w.runBatch();
 assert.match(d.getElementById('bsummary').textContent,/Error/); assert.equal(d.getElementById('bexport').disabled,true);
 assert.equal(d.querySelectorAll('#batchresults tr').length,0);
 d.getElementById('btext').value='[{"text":"Cotton kurti","expected_category":"fashion"}]'; failBatch=true; await w.runBatch();
 assert.match(d.getElementById('bsummary').textContent,/invalid batch/);
 d.getElementById('psource').value='deals'; d.getElementById('ctext').value='Cotton kurti'; await w.previewCategory();
 assert.match(d.getElementById('cresult').textContent,/Channel checks: @ClothingShelf: candidate/);
 // Editing while an old request is in flight must not show/export old results.
 failBatch=false; let release; holdBatch=new Promise(resolve=>release=resolve);
 const pending=w.runBatch();
 d.getElementById('btext').value='[{"text":"New caption"}]';
 d.getElementById('btext').dispatchEvent(new w.Event('input'));
 release(); await pending; holdBatch=null;
 assert.equal(d.getElementById('bexport').disabled,true);
 assert.equal(d.querySelectorAll('#batchresults tr').length,0);
 assert.match(d.getElementById('bsummary').textContent,/Input changed/);
 w.confirm=()=>true; w.loadBatchExamples();
 const examples=JSON.parse(d.getElementById('btext').value);
 assert.equal(examples.length,5); assert.equal(examples[0].expected_category,'fashion');
 console.log('Dashboard DOM PASS: visual editor, required selection, stale save, source-aware batch, safe text, filters, error clearing, single preview.');
 w.close();
})().catch(e=>{console.error(e);process.exit(1)});
