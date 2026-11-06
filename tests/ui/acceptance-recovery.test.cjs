const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {parseHTML}=require('linkedom');
const root='1'.repeat(32),next='2'.repeat(32);
function setup(){
 const {window,document}=parseHTML(fs.readFileSync('app/static/index.html','utf8'));
 let active=null,now=Date.parse('2026-11-01T00:00:00Z');Object.defineProperty(document,'activeElement',{get:()=>active});window.HTMLElement.prototype.focus=function(){active=this;};window.scrollTo=()=>{};
 document.getElementById('task-form').elements=Object.fromEntries([...document.querySelectorAll('#task-form [name]')].map(n=>[n.getAttribute('name'),n]));
 window.markdownit=()=>({renderer:{rules:{}},utils:{escapeHtml:x=>x},render:x=>x});
 const fixture={id:root,prompt:'Synthetic goal',created:'2026-11-01T00:00:00Z',state:'running',llm_calls:7,llm_limit:20,agent_limit:10,command_limit:12,command_budget:{used:3,limit:12,remaining:9,work_remaining:6,review_remaining:3,reserve:3},agents:[{id:'reviewer',agent:'swarm_reviewer',parent:'coordinator',state:'completed'}],events:[],files:[],codex_jobs:[]};
 const data={list:[fixture],details:{[root]:fixture},requests:[]};
 const location={hash:'',pathname:'/'};
 const context=vm.createContext({window,document,location,history:{pushState(_a,_b,path){location.hash=path.startsWith('#')?path:'';},replaceState(_a,_b,path){location.hash=path.startsWith('#')?path:'';}},matchMedia:()=>({matches:false,addEventListener(){}}),setTimeout:()=>{},clearTimeout(){},setInterval:()=>{},navigator:{},console,Date:class extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}},fetch:async(path,options)=>{data.requests.push({path,options});const value=path==='/api/health'?{ready:true,codex:{ready:true}}:path==='/api/tasks'?data.list:options?.method==='POST'?{}:data.details[path.split('/').at(-1)];if(!value)throw new Error('Unexpected fixture URL '+path);return {ok:true,json:async()=>value};}});
 vm.runInContext(fs.readFileSync('app/static/app.js','utf8').replace('showTask(selected,false);setInterval(refresh,2000);',''),context);
 context.fixture=fixture;vm.runInContext('tasks=[fixture];selected=fixture.id',context);
 return {document,window,fixture,data,location,run:code=>vm.runInContext(code,context),advance:ms=>{now+=ms;}};
}
function acceptance(status){return {status,summary:'Проверено 2 файла',criteria:['Тесты проходят','Нет пропущенных строк'],job_id:'validation-job',flow:'reviewer',checked_at:'2026-11-01T00:00:00Z',artifacts:[{path:'outputs/report.md',sha256:'a'.repeat(64)},{path:'outputs/data.csv',sha256:'b'.repeat(64)}],command:'python check.py',exit_code:0,source:'runtime_validation',reason:''};}
function recovery(status){return {enabled:true,status,attempt:1,max_attempts:3,retry_at:'2026-11-01T00:00:15Z',reason:'Временный ответ 429',next_task_id:null};}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
for(const [status,label] of [['unverified',/не подтверждена/],['checking',/Проверка/],['passed',/подтверждена/],['failed',/не пройдена/],['stale',/устарела/]]){
 test('acceptance renders authoritative '+status+' independently of command success',()=>{
  const {fixture,document,run}=setup();fixture.state='completed';fixture.acceptance=acceptance(status);fixture.result='ПРИНЯТО';fixture.codex_jobs=[{id:'validation-job',kind:'command',state:'completed',result:'{"exit_code":0}'}];run('renderTask(fixture)');
  const badge=document.getElementById('acceptance-status');assert.equal(badge.dataset.state,status);assert.match(badge.textContent,label);
 });
}
test('missing or unknown acceptance never turns result text or exit zero into approval',()=>{
 const {fixture,document,run}=setup();fixture.state='completed';fixture.result='ПРИНЯТО';fixture.codex_jobs=[{id:'success',kind:'command',state:'completed',result:'{"exit_code":0}'}];
 for(const value of [undefined,{status:'approved',exit_code:0}]){fixture.acceptance=value;run('renderTask(fixture)');assert.equal(document.getElementById('acceptance-status').dataset.state,'unverified');}
});
test('acceptance shows criteria, scope, evidence and safe text while retaining focused evidence details on polls',()=>{
 const {fixture,document,run}=setup();fixture.acceptance=acceptance('passed');fixture.acceptance.criteria.push('<img src="https://example.test/x" onerror="alert(1)">');fixture.acceptance.reason='api_key=supersecret';run('renderTask(fixture)');
 const section=document.querySelector('.acceptance-section');assert.match(section.textContent,/Нет пропущенных строк/);assert.match(section.textContent,/Объём проверки: 2/);assert.match(section.textContent,/validation-job/);assert.match(section.textContent,/reviewer/);assert.match(section.textContent,/python check.py/);assert.match(section.textContent,/Код завершения:\s*0/);assert.doesNotMatch(section.textContent,/supersecret/);assert.equal(section.querySelector('img,script'),null);
 const details=section.querySelector('details'),summary=details.querySelector('summary');details.open=true;summary.focus();fixture.llm_calls++;run('renderTask(fixture)');assert.ok(details.isConnected);assert.equal(details.open,true);assert.equal(document.activeElement,summary);
});
test('evidence opens an older validation job outside the recent command window',()=>{
 const {fixture,document,window,run}=setup();fixture.acceptance=acceptance('passed');fixture.codex_jobs=Array.from({length:30},(_,i)=>({id:i?'job-'+i:'validation-job',kind:'command',created:new Date(Date.UTC(2026,10,1,0,0,i)).toISOString(),state:'completed',result:'{"exit_code":0}'}));run('renderTask(fixture)');
 const button=document.getElementById('acceptance-job');assert.ok(button,'evidence action is available');button.dispatchEvent(new window.Event('click'));
 const row=document.querySelector('#command-jobs [data-job="validation-job"]');assert.ok(row);assert.equal(row.open,true);assert.equal(document.activeElement,row.querySelector('summary'));assert.equal(document.getElementById('panel-team').classList.contains('hidden'),false);
});
test('scheduled recovery is active, cancellable and exposes countdown plus remaining budgets',async()=>{
 const {fixture,document,window,data,run,advance}=setup();fixture.state='failed';fixture.recovery=recovery('scheduled');run('renderTask(fixture);renderList()');
 const stop=document.getElementById('stop-task'),panel=document.getElementById('recovery-section');assert.ok(panel&&!panel.classList.contains('hidden'));assert.equal(stop.classList.contains('hidden'),false);assert.equal(document.getElementById('resume-task').classList.contains('hidden'),true);assert.equal(document.getElementById('active-count').textContent,'1');assert.match(panel.textContent,/429/);assert.match(panel.textContent,/15 с/);assert.match(panel.textContent,/2 из 3/);assert.match(panel.textContent,/GLM: 13/);assert.match(panel.textContent,/агентов: 9/);assert.match(panel.textContent,/команд: 9/);
 stop.focus();advance(5000);run('renderTask(fixture)');assert.match(panel.textContent,/10 с/);assert.equal(document.activeElement,stop);advance(20000);run('renderTask(fixture)');assert.match(panel.textContent,/Ожидаем запуска/);assert.doesNotMatch(panel.textContent,/-10 с/);
 stop.dispatchEvent(new window.Event('click'));await flush();assert.ok(data.requests.some(r=>r.path==='/api/tasks/'+root+'/stop'&&r.options?.method==='POST'));
});
for(const [status,label] of [['idle',/включено/],['continued',/Продолжение/],['exhausted',/исчерпаны/],['blocked',/невозможно/],['disabled',/отключено/]]){
 test('recovery explains '+status,()=>{const {fixture,document,run}=setup();fixture.recovery=recovery(status);run('renderTask(fixture)');const panel=document.getElementById('recovery-section');assert.ok(panel);assert.match(panel.textContent,label);assert.match(panel.textContent,/429/);});
}
test('polling follows automatic continuation within one goal without resetting the current tab',async()=>{
 const {fixture,document,data,location,run}=setup();fixture.state='failed';fixture.recovery=recovery('scheduled');await run('refresh()');run("setTab('activity')");
 const child={...fixture,id:next,state:'running',resumed_from:root,recovery:recovery('idle')};fixture.resumed_as=next;fixture.recovery={...recovery('continued'),next_task_id:next};data.list.push(child);data.details[next]=child;
 await run('refresh()');assert.equal(run('selected'),next);assert.equal(location.hash,'#'+next);assert.equal(document.getElementById('panel-activity').classList.contains('hidden'),false);assert.equal(document.querySelectorAll('#task-list .task-item').length,1);assert.equal(document.querySelectorAll('#run-history button').length,2);
});
test('explicit history selection is never overridden by continuation polling',async()=>{
 const {fixture,document,window,data,run}=setup();const child={...fixture,id:next,state:'running',resumed_from:root,recovery:recovery('idle')};fixture.state='failed';fixture.resumed_as=next;fixture.recovery={...recovery('continued'),next_task_id:next};data.list.push(child);data.details[next]=child;await run('refresh()');
 const historical=document.querySelector('#run-history [data-run="'+root+'"]');historical.dispatchEvent(new window.Event('click'));await flush();await run('refresh()');assert.equal(run('selected'),root);assert.equal(document.querySelector('#run-history [aria-current="true"]').dataset.run,root);
});
test('recovery-only list updates retain keyboard focus and show waiting state',()=>{
 const {fixture,document,run}=setup();fixture.state='failed';fixture.recovery=recovery('idle');run('renderList()');const button=document.querySelector('#task-list button');button.focus();fixture.recovery=recovery('scheduled');run('renderList()');assert.ok(button.isConnected);assert.equal(document.activeElement,button);assert.match(button.textContent,/восстановления/);
});
test('opening a live task by URL follows its later continuation',async()=>{
 const {fixture,data,run}=setup();run('showTask(fixture.id,false)');await flush();
 const child={...fixture,id:next,resumed_from:root};fixture.state='failed';fixture.resumed_as=next;fixture.recovery={...recovery('continued'),next_task_id:next};data.list.push(child);data.details[next]=child;await run('refresh()');assert.equal(run('selected'),next);
});
test('opening an already historical task by URL keeps that run selected',async()=>{
 const {fixture,data,run}=setup();const child={...fixture,id:next,resumed_from:root};fixture.state='failed';fixture.resumed_as=next;fixture.recovery={...recovery('continued'),next_task_id:next};data.list.push(child);data.details[next]=child;run('showTask(fixture.id,false)');await flush();await run('refresh()');assert.equal(run('selected'),root);
});
test('reviewer details show only acceptance attributed to that workflow',()=>{
 const {fixture,document,run}=setup();fixture.acceptance=acceptance('passed');run("openAgent(fixture,'reviewer')");assert.match(document.getElementById('agent-detail-body').textContent,/Приёмка подтверждена/);fixture.acceptance.flow='another-reviewer';run('renderAgentDetails(fixture)');assert.doesNotMatch(document.getElementById('agent-detail-body').textContent,/Приёмка подтверждена/);
});
test('historical source counts the automatic continuation it already created',()=>{
 const {fixture,document,run}=setup();fixture.state='failed';fixture.recovery={...recovery('continued'),attempt:0,next_task_id:next};run('renderTask(fixture)');const text=document.getElementById('recovery-timing').textContent;assert.match(text,/Использовано попыток: 1 из 3/);assert.match(text,/Осталось: 2/);
});
