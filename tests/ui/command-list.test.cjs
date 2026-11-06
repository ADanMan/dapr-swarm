// Run with npm run test:ui after npm ci --ignore-scripts.
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const {parseHTML}=require('linkedom');
function setup(){
 const {window,document}=parseHTML(fs.readFileSync('app/static/index.html','utf8'));
 let active=null;Object.defineProperty(document,'activeElement',{get:()=>active});window.HTMLElement.prototype.focus=function(){active=this;};
 document.getElementById('task-form').elements=Object.fromEntries([...document.querySelectorAll('#task-form [name]')].map(n=>[n.getAttribute('name'),n]));
 window.markdownit=()=>({renderer:{rules:{}},utils:{escapeHtml:x=>x},render:x=>x});
 const context=vm.createContext({window,document,location:{hash:'',pathname:'/'},history:{pushState(){}},matchMedia:()=>({matches:false,addEventListener(){}}),setTimeout,clearTimeout,setInterval:()=>{},navigator:{},console});
 vm.runInContext(fs.readFileSync('app/static/app.js','utf8').replace('showTask(selected,false);setInterval(refresh,2000);',''),context);
 const task={id:'fixture',agents:[],events:[],codex_jobs:Array.from({length:1000},(_,i)=>({id:'command-'+i,kind:'command',created:new Date(Date.UTC(2026,10,1,0,0,i)).toISOString(),state:'completed',result:JSON.stringify({exit_code:i===999?1:0,stdout:'output '+i})}))};
 context.fixture=task;const render=()=>vm.runInContext('renderTeam(fixture)',context);render();return {document,window,task,render};
}
test('large command history initially shows the recent twenty and can be expanded',()=>{
 const {document,window,render}=setup(),container=document.getElementById('command-jobs');
 assert.equal(container.querySelectorAll('details').length,20);
 assert.equal(container.querySelector('details').dataset.job,'command-999');
 const more=[...container.querySelectorAll('button')].find(b=>b.textContent.includes('Показать ещё'));
 assert.ok(more);more.focus();more.dispatchEvent(new window.Event('click'));
 assert.equal(container.querySelectorAll('details').length,40);
 assert.match(document.activeElement.textContent,/Показать ещё/);
 render();assert.equal(container.querySelectorAll('details').length,40);
});
test('nonzero exit is a command error even when the worker transport completed',()=>{
 const {document}=setup(),row=document.querySelector('#command-jobs [data-job="command-999"]');
 assert.equal(row.querySelector('.status').dataset.state,'failed');
 assert.match(row.textContent,/Ошибка команды/);assert.match(row.textContent,/Код завершения: 1/);
 const success=document.querySelector('#command-jobs [data-job="command-998"]');
 assert.match(success.querySelector('.status').textContent,/Код 0/);
});
test('polling preserves opened output, keyboard focus and a row pushed beyond the recent window',()=>{
 const {document,task,render}=setup(),container=document.getElementById('command-jobs');
 const row=container.querySelector('[data-job="command-980"]');row.open=true;row.querySelector('summary').focus();
 const previousFocus=document.activeElement;
 task.codex_jobs.push({id:'new-command',kind:'command',created:'2026-11-01T01:00:00Z',state:'running',result:''});render();
 assert.ok(container.querySelector('[data-job="command-980"]')===row,'retains the same DOM row');
 assert.equal(row.open,true);assert.ok(document.activeElement===previousFocus,'retains keyboard focus');
 assert.equal(container.querySelectorAll('details').length,21);
});

test('transport completion without an exit code remains unconfirmed',()=>{
 const {document,task,render}=setup();task.codex_jobs.at(-1).result='{"stdout":"worker response"}';render();
 const status=document.querySelector('#command-jobs [data-job="command-999"] .status');
 assert.equal(status.dataset.state,'unverified');assert.equal(status.textContent,'Код не подтверждён');
});
