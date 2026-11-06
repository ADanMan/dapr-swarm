const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {parseHTML}=require('linkedom');
function setup(){
 const {window,document}=parseHTML(fs.readFileSync('app/static/index.html','utf8'));
 let active=null;Object.defineProperty(document,'activeElement',{get:()=>active});window.HTMLElement.prototype.focus=function(){active=this;};
 document.getElementById('task-form').elements=Object.fromEntries([...document.querySelectorAll('#task-form [name]')].map(n=>[n.getAttribute('name'),n]));
 window.markdownit=()=>({renderer:{rules:{}},utils:{escapeHtml:x=>x},render:x=>x});
 const context=vm.createContext({window,document,location:{hash:'',pathname:'/'},history:{pushState(){}},matchMedia:()=>({matches:false,addEventListener(){}}),setTimeout,clearTimeout,setInterval:()=>{},navigator:{},console,fetch:async()=>({ok:true,text:async()=>'Synthetic preview'})});
 vm.runInContext(fs.readFileSync('app/static/app.js','utf8').replace('showTask(selected,false);setInterval(refresh,2000);',''),context);
 const task={id:'fixture',prompt:'Synthetic goal',created:'2026-11-01T00:00:00Z',state:'running',llm_calls:1,agents:[{id:'agent-123',agent:'swarm_analyst',parent:null,state:'running',assignment:'Сверить таблицу'}],events:[{id:1,flow:'agent-123',kind:'message',created:'2026-11-01T00:00:00Z',content:'Synthetic detail '.repeat(40)}],files:[{path:'outputs/report.md',size:100},{path:'inputs/data.csv',size:50}],codex_jobs:[]};
 context.fixture=task;vm.runInContext('tasks=[fixture]',context);return {document,window,task,run:code=>vm.runInContext(code,context)};
}
test('assigned agents remain searchable by id, machine role and localized role',()=>{
 const {document,run}=setup();for(const query of ['agent-123','swarm_analyst','Аналитик']){document.getElementById('team-search').value=query;run("renderHierarchy(fixture,$('agents'))");assert.equal(document.querySelectorAll('#agents [data-flow]').length,1,query);}
});
test('run history preserves its focused button across unchanged and state-changing polls',()=>{
 const {document,task,run}=setup();run('renderRunHistory(fixture)');const button=document.querySelector('#run-history button');button.focus();run('renderRunHistory(fixture)');assert.ok(button.isConnected,'unchanged history keeps button');task.state='completed';run('renderRunHistory(fixture)');assert.ok(button.isConnected,'updated state keeps button');assert.ok(document.activeElement===button);assert.match(button.textContent,/Исполнение завершено/);
});
test('journal and agent details retain open focused event on new events',()=>{
 const {document,task,run}=setup();run('renderEvents(fixture)');let summary=document.querySelector('#events summary');summary.parentElement.open=true;summary.focus();task.events.push({...task.events[0],id:2});run('renderEvents(fixture)');assert.ok(document.activeElement.isConnected);assert.equal(document.activeElement.parentElement.dataset.event,'1');assert.equal(document.activeElement.parentElement.open,true);
 run("openAgent(fixture,'agent-123')");summary=document.querySelector('#agent-detail-body summary');summary.parentElement.open=true;summary.focus();task.events.push({...task.events[0],id:3});run('renderAgentDetails(fixture)');assert.ok(document.activeElement.isConnected);assert.equal(document.activeElement.parentElement.dataset.event,'1');assert.equal(document.activeElement.parentElement.open,true);
});
test('new files preserve expanded groups, loaded preview, scroll and focus',async()=>{
 const {document,task,run,window}=setup();run('renderFiles(fixture)');const groups=[...document.querySelectorAll('#artifacts .file-group')],input=groups.find(g=>g.textContent.includes('Входные материалы'));input.open=true;input.dispatchEvent(new window.Event('toggle'));
 const preview=input.querySelector('.preview');preview.open=true;preview.dispatchEvent(new window.Event('toggle'));await new Promise(resolve=>setImmediate(resolve));const pre=preview.querySelector('pre');assert.equal(pre.textContent,'Synthetic preview');pre.scrollTop=37;preview.querySelector('summary').focus();
 task.files.push({path:'outputs/new.txt',size:30});run('renderFiles(fixture)');assert.ok(input.isConnected,'group node retained');assert.equal(input.open,true);assert.ok(preview.isConnected,'preview node retained');assert.equal(preview.open,true);assert.equal(pre.textContent,'Synthetic preview');assert.equal(pre.scrollTop,37);assert.ok(document.activeElement.isConnected);assert.ok(document.activeElement===preview.querySelector('summary'));
});
