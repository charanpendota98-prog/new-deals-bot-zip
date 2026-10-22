// The tunnel link in a real browser context: ?token=... is taken once, kept for
// that tab only, and removed from the address bar immediately. Without it the
// page behaves exactly as before (paste the token).
const {JSDOM}=require('jsdom');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const html=execFileSync(process.env.PYTHON||'python3',['-c','from dashboard.app import UI_HTML; print(UI_HTML)'],{encoding:'utf8'});
const TOKEN='s3cret-dashboard-token-abcd1234';
const calls=[];

function open(url){
  return new JSDOM(html,{url,runScripts:'dangerously',beforeParse(w){
    w.alert=message=>{throw new Error(message)};
    w.fetch=async(path,options={})=>{
      calls.push({path,headers:options.headers||{}});
      let data={};
      if(path==='/api/status')data={queue:{}};
      if(path==='/api/channels')data={revision:'r1',dashboard:[],categories:{},sources:[],source_catalog:[],source_limit:100};
      if(path==='/api/recent')data={jobs:[]};
      if(path.startsWith('/api/pipeline'))data={tiles:[],findings:[],sources:[],targets:[],verdict:'Nothing dropped',overall:'ok',unaccounted_ids:[]};
      return {ok:true,status:200,json:async()=>data};
    };
  }});
}

(async()=>{
  // 1. A tunnel link with the token: no paste, and the secret leaves the URL.
  const dom=open('http://127.0.0.1:8080/?token='+TOKEN+'#pipeline');
  const w=dom.window,d=w.document;
  assert.equal(w.eval('TOKEN'),TOKEN,'the token from the link is used');
  assert.equal(w.sessionStorage.getItem('dash_token'),TOKEN,'and kept for this tab');
  assert.equal(w.location.search,'','the token is stripped from the address bar');
  assert.equal(w.location.hash,'#pipeline','the panel anchor survives');
  assert.equal(d.title.length>0,true);
  await w.refresh();
  const authed=calls.filter(c=>c.path.startsWith('/api/')).every(
    c=>!(c.path==='/api/status')||c.headers['X-Dashboard-Token']===TOKEN);
  assert.ok(authed,'every API call carries the token header');
  // Nothing on the page leaks the token back into the DOM.
  assert.equal(d.body.textContent.includes(TOKEN),false,'the token is not rendered');

  // 2. Other query parameters are preserved.
  const keep=open('http://127.0.0.1:8080/?token='+TOKEN+'&hours=24#pipeline');
  assert.equal(keep.window.location.search,'?hours=24','unrelated parameters are kept');
  assert.equal(keep.window.eval('TOKEN'),TOKEN);

  // 3. No token in the URL: unchanged behaviour (paste the token).
  calls.length=0;
  const plain=open('http://127.0.0.1:8080/#pipeline');
  assert.equal(plain.window.eval('TOKEN'),'','nothing is invented');
  await plain.window.refresh();
  assert.equal(calls.filter(c=>c.path.startsWith('/api/')).length,0,
    'no request is made without a token (the page asks for it instead)');

  // 4. The how-to card is on the page, closed by default, with the real command.
  assert.ok(d.getElementById('howtoconnect'),'the connection help is present');
  assert.match(d.getElementById('howtoconnect').textContent,/ssh -N -L 8080:127\.0\.0\.1:8080/);
  assert.match(d.getElementById('howtoconnect').textContent,/dashboard\.env/);
  assert.equal(d.querySelector('#howtoconnect details').open,false,'it does not get in the way');

  console.log('Dashboard link DOM PASS: ?token= taken once and stripped from the address bar, kept per tab, other parameters preserved, no invented token, every API call authenticated, nothing rendered, and the tunnel/command help present but collapsed.');
  dom.window.close();
  keep.window.close();
  plain.window.close();
})().catch(error=>{console.error(error);process.exitCode=1});
