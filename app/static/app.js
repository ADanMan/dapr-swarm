'use strict';
const $ = id => document.getElementById(id);
const stateNames = {preparing:'Сохранение файлов',queued:'В очереди',running:'В работе',finishing:'Ожидает сверки',completed:'Исполнение завершено',failed:'Ошибка',stopped:'Остановлена',suspended:'Приостановлена',model:'Ответ GLM',working:'Выполнение',waiting_children:'Ожидание детей'};
const roles = {coordinator:['Координатор','Планирование и сборка результата','activity'],researcher:['Исследователь','Поиск и проверка источников','globe'],analyst:['Аналитик','Разбор материалов и выводы','file'],builder:['Автор / программист','Документы, код и запуск тестов','code'],reviewer:['Проверяющий','Проверка готового результата','shield']};
roles.swarm_coordinator=roles.coordinator;roles.swarm_lead=['Руководитель направления','Декомпозиция и управление дочерними агентами','activity'];for(const role of ['researcher','analyst','builder','reviewer'])roles['swarm_'+role]=roles[role];
const eventNames = {resume:'Продолжение задачи',agent:'Исполнитель',thinking:'Обращение к GLM',tool:'Инструмент',message:'Ответ',search:'Поиск',page:'Источник',file:'Файл сохранён',plan:'План работы',retry:'Повтор запроса',error:'Ошибка',codex:'Codex · GPT-6 Luna',command:'Команда GLM'};
const paths = {plus:'M12 5v14M5 12h14',search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',menu:'M4 6h16M4 12h16M4 18h16',arrow:'M5 12h14M13 6l6 6-6 6',chevron:'M6 9l6 6 6-6',paperclip:'M21 11l-9 9a6 6 0 0 1-8-8l10-10a4 4 0 0 1 6 6L10 18a2 2 0 0 1-3-3l9-9',file:'M14 2H5v20h14V7Z M14 2v6h5M8 13h8M8 17h6',code:'M8 5l-6 7 6 7M16 5l6 7-6 7M14 3l-4 18',globe:'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0M2 12h20M12 2c6 5 6 15 0 20-6-5-6-15 0-20',shield:'M12 2l9 4v6c0 5-9 10-9 10S3 17 3 12V6ZM8 12l3 3 5-6',monitor:'M2 3h20v14H2ZM8 21h8M12 17v4',sliders:'M4 5h16M4 12h16M4 19h16M8 3v4M16 10v4M10 17v4',copy:'M9 9h12v12H9ZM5 15H3V3h12v2',stop:'M6 6h12v12H6Z',close:'M6 6l12 12M18 6 6 18',download:'M12 3v12M6 9l6 6 6-6M4 16v5h16v-5',activity:'M2 12h5l3-8 4 16 3-8h5',check:'M5 12l4 4L19 6'};
function element(tag,text,className){const n=document.createElement(tag);if(text!==undefined)n.textContent=safeText(text);if(className)n.className=className;return n;}
function icon(name){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d',paths[name]||paths.activity);svg.append(p);return svg;}
document.querySelectorAll('[data-icon]').forEach(n=>n.append(icon(n.dataset.icon)));
const md=window.markdownit({html:false,linkify:true,typographer:false});
md.renderer.rules.image=(tokens,index)=>md.utils.escapeHtml(tokens[index].content||'Изображение');
let selected=/^#[a-f0-9]{32}$/.test(location.hash)?location.hash.slice(1):null, currentTask=null, tasks=[], attached=[], filter='all', activeTab='overview', busy=false, submitting=false, healthy=false;
let detailSignature='', listSignature='', filesSignature='', toastTimer, followContinuation=!selected;
const jobListViews=new WeakMap(),renderSignatures=new WeakMap(),agentDetailViews=new WeakMap(),fileViews=new WeakMap();
const expandedFlows=new Set();let selectedAgent=null,agentReturnKey=null,lastUpdated=null;
const activeStates=['preparing','queued','running','suspended'];
function textNotice(id,text){$(id).textContent=safeText(text);$(id).classList.toggle('hidden',!text);}
function toast(text){textNotice('toast',text);clearTimeout(toastTimer);toastTimer=setTimeout(()=>textNotice('toast',''),2600);}
function shortTitle(text,max=115){const first=text.split('\n')[0].trim();return first.length>max?first.slice(0,max).replace(/\s+\S*$/,'')+'…':first;}
function date(value){return new Date(value).toLocaleString('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});}
function size(value){return value<1024?value+' Б':value<1048576?Math.ceil(value/1024)+' КБ':(value/1048576).toFixed(1)+' МБ';}
const mobileLayout=matchMedia('(max-width:820px)');
function setSidebar(open){open=Boolean(open&&mobileLayout.matches);$('sidebar').inert=mobileLayout.matches&&!open;$('main').inert=open;$('sidebar').classList.toggle('open',open);$('sidebar-backdrop').classList.toggle('hidden',!open);$('menu-toggle').setAttribute('aria-expanded',String(open));if(open)$('task-search').focus();}
mobileLayout.addEventListener('change',()=>setSidebar(false));
$('close-sidebar').addEventListener('click',()=>{setSidebar(false);$('menu-toggle').focus();});
document.querySelector('.skip-link').addEventListener('click',e=>{e.preventDefault();$('main').focus();});
function setTab(name,focus=false){activeTab=name;document.querySelectorAll('[data-tab]').forEach(b=>{const on=b.dataset.tab===name;b.setAttribute('aria-selected',String(on));b.tabIndex=on?0:-1;$('panel-'+b.dataset.tab).classList.toggle('hidden',!on);if(on&&focus)b.focus();});}
function showTask(id,push=true,follow=null){closeAgent();selected=id;followContinuation=follow??(push?true:null);currentTask=null;detailSignature='';listSignature='';filesSignature='';if(push)history.pushState(null,'',id?'#'+id:location.pathname);$('composer').classList.toggle('hidden',Boolean(id));$('task-detail').classList.add('hidden');$('loading').classList.toggle('hidden',!id);$('page-label').textContent=id?'Задача':'Новая задача';setTab('overview');setSidebar(false);if(!id)$('prompt').focus();window.scrollTo({top:0,behavior:'instant'});renderList();refresh();}
async function api(path,options){const r=await fetch(path,options);const j=await r.json();if(!r.ok)throw new Error(typeof j.detail==='string'?j.detail:'Проверь поля формы и вложения.');return j;}
function empty(title,description,name='file'){const box=element('div',undefined,'empty-state');box.append(icon(name),element('b',title),element('p',description));return box;}
function badge(value){const n=element('span',stateNames[value]||value,'status');n.dataset.state=value;return n;}
function downloadUrl(task,file){return '/api/tasks/'+task.id+'/files/'+file.path.split('/').map(encodeURIComponent).join('/');}
function fileRow(task,file,preview=false){const row=element('div',undefined,'file-row');const mark=element('span',undefined,'file-icon');mark.append(icon(/\.(py|js|ts|html|css|go|rs)$/.test(file.path)?'code':'file'));const body=element('div',undefined,'file-name');body.append(element('b',file.path.split('/').pop()),element('small',file.path+' · '+size(file.size)));const link=element('a',undefined,'download');link.href=downloadUrl(task,file);link.download=file.path.split('/').pop();link.setAttribute('aria-label','Скачать '+file.path);link.append(icon('download'),element('span','Скачать'));row.append(mark,body,link);if(preview&&/\.(txt|md|json|csv|py|js|ts|html|css|xml|yaml|yml|go|rs)$/i.test(file.path)&&file.size<524288){const d=element('details',undefined,'preview');d.append(element('summary','Посмотреть содержимое'));const pre=element('pre','Загрузка…');d.append(pre);body.append(d);let loaded=false;d.addEventListener('toggle',async()=>{if(!d.open||loaded)return;loaded=true;try{const r=await fetch(link.href);if(!r.ok)throw new Error('Не удалось прочитать файл.');pre.textContent=safeText((await r.text()).slice(0,50000));}catch(e){pre.textContent=e.message;loaded=false;}});}return row;}
function renderMarkdown(task){$('result').innerHTML=md.render(safeText(task.result||''));for(const a of $('result').querySelectorAll('a')){const href=a.getAttribute('href')||'';const file=task.files.find(f=>href===f.path||href.endsWith('/workspaces/'+task.id+'/'+f.path));if(file){a.href=downloadUrl(task,file);a.download=file.path.split('/').pop();}else if(/^https?:\/\//i.test(href)){a.target='_blank';a.rel='noopener noreferrer';}else a.removeAttribute('href');}}
function reconcileChildren(container,nodes){
  const wanted=new Set(nodes);for(const child of [...container.children])if(!wanted.has(child))child.remove();
  nodes.forEach((node,index)=>{if(container.children[index]!==node)container.insertBefore(node,container.children[index]||null);});
}
function captureFocus(container){
  const active=document.activeElement,key=container.contains(active)?active?.dataset.focusKey:null;
  return ()=>{if(key){const target=[...container.querySelectorAll('[data-focus-key]')].find(node=>node.dataset.focusKey===key);if(target&&document.activeElement!==target)target.focus({preventScroll:true});}};
}
function renderEvents(task){
  const container=$('events'),restore=captureFocus(container);if(container.dataset.task!==task.id){container.replaceChildren();container.dataset.task=task.id;}
  const existing=new Map([...container.querySelectorAll('[data-event-row]')].map(row=>[row.dataset.eventRow,row]));
  if(!task.events.length){reconcileChildren(container,[empty('Работа ещё не началась','Здесь будут план, действия агентов, поиск и повторные запросы.','activity')]);return;}
  const names=Object.fromEntries(task.agents.map(a=>[a.id,(roles[a.agent]||[a.agent])[0]])),nodes=[];
  for(const e of task.events){
    const key=String(e.id),signature=JSON.stringify([e,names[e.flow]]),previous=existing.get(key);
    if(previous&&renderSignatures.get(previous)===signature){nodes.push(previous);continue;}
    const row=element('div',undefined,'event'),i=element('span',undefined,'event-icon');row.dataset.eventRow=key;
    i.append(icon({search:'search',page:'globe',file:'file',codex:'code',error:'stop',plan:'sliders',message:'check'}[e.kind]||'activity'));
    row.append(element('time',new Date(e.created).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})),i);
    const body=element('div');body.append(element('b',(eventNames[e.kind]||e.kind)+' · '+(names[e.flow]||'Агент')));let content=safeText(e.content);
    if(e.kind==='plan'){try{content=JSON.parse(content).map((s,index)=>(index+1)+'. '+s).join('\n');}catch{}}
    if(e.kind==='search'){try{const p=JSON.parse(content);content=p.query+'\n\n'+p.results.map(r=>r.title+'\n'+r.url).join('\n\n');}catch{}}
    if(content.length>360){const d=element('details'),summary=element('summary',content.slice(0,140)+'…');d.dataset.event=key;d.open=Boolean(previous?.querySelector('details')?.open);summary.dataset.focusKey='events:'+task.id+':'+key;d.append(summary,element('p',content));body.append(d);}else body.append(element('p',content));
    row.append(body);renderSignatures.set(row,signature);nodes.push(row);
  }
  reconcileChildren(container,nodes);restore();
}
function renderAgentDetails(task){
  if(!selectedAgent)return;const agent=task.agents.find(a=>a.id===selectedAgent);if(!agent){closeAgent();return;}
  $('agent-detail-title').textContent=safeText(flowLabel(agent));const body=$('agent-detail-body'),restore=captureFocus(body),key=task.id+':'+agent.id;
  let view=agentDetailViews.get(body);if(!view||view.key!==key){view={key,header:element('div'),events:element('div')};agentDetailViews.set(body,view);body.replaceChildren(view.header,view.events);}
  const fields=[['Поручение',agent.assignment||'Поручение не сохранено в структурированных данных.'],['Родитель',agent.parent?flowLabel(task.agents.find(a=>a.id===agent.parent)||{id:agent.parent,agent:'Агент'}):'Корень запуска'],['Модель','GLM'],['Ожидаемые файлы',Array.isArray(agent.expected_outputs)?agent.expected_outputs.join('\n'):'Не указаны в данных.'],['Приёмка',task.acceptance?.flow===agent.id?acceptanceSummary(task).label:'Для этого исполнителя подтверждение не зарегистрировано.'],['Workflow',agent.id]];
  const headerSignature=JSON.stringify([fields,flowState(agent)]);if(view.signature!==headerSignature){const dl=element('dl');for(const [name,value] of fields)dl.append(element('dt',name),element('dd',value));view.header.replaceChildren(badge(flowState(agent)),dl,element('h3','События исполнителя'));view.signature=headerSignature;}
  const existing=new Map([...view.events.querySelectorAll('details')].map(node=>[node.dataset.event,node])),all=task.events.filter(e=>e.flow===agent.id),recent=new Set(all.slice(-12).map(e=>String(e.id)));
  const events=all.filter(e=>recent.has(String(e.id))||existing.get(String(e.id))?.open||existing.get(String(e.id))?.contains(document.activeElement)),nodes=[];
  if(!events.length)nodes.push(element('p','В доступном фрагменте журнала нет событий этого исполнителя.','muted'));
  for(const e of events){const id=String(e.id);let row=existing.get(id);if(!row){row=element('details',undefined,'agent-event');row.dataset.event=id;const summary=element('summary');summary.dataset.focusKey='agent-event:'+key+':'+id;row.append(summary,element('pre'));}const signature=JSON.stringify(e);if(renderSignatures.get(row)!==signature){row.querySelector('summary').textContent=date(e.created)+' · '+(eventNames[e.kind]||e.kind);row.querySelector('pre').textContent=safeText(e.content);renderSignatures.set(row,signature);}nodes.push(row);}
  reconcileChildren(view.events,nodes);restore();
}
function renderRunHistory(task){
  const container=$('run-history'),restore=captureFocus(container),group=groupGoalRuns(tasks).find(g=>g.runs.some(r=>r.id===task.id)),runs=group?.runs||[task];
  const existing=new Map([...container.querySelectorAll('[data-run]')].map(node=>[node.dataset.run,node])),nodes=[];
  for(const [i,run] of [...runs].sort((a,b)=>a.created.localeCompare(b.created)).entries()){
    let button=existing.get(run.id);if(!button){button=element('button');button.dataset.run=run.id;button.dataset.focusKey='run:'+run.id;button.addEventListener('click',()=>showTask(run.id,true,false));}
    const label='Запуск '+(i+1)+' · '+(stateNames[run.state]||run.state);if(button.textContent!==label)button.textContent=label;button.setAttribute('aria-current',run.id===task.id?'true':'false');nodes.push(button);
  }
  reconcileChildren(container,nodes);restore();$('goal-budget').textContent=runs.length>1?'По цели: '+runs.length+' запуска · '+runs.reduce((n,r)=>n+(r.llm_calls||0),0).toLocaleString('ru-RU')+' обращений GLM суммарно. Автовосстановление использует остаток бюджета; ручное продолжение задаёт новые лимиты.':'';
}
function renderFiles(task){
  const fileSig=JSON.stringify([task.id,task.files]);if(fileSig===filesSignature)return;filesSignature=fileSig;
  const container=$('artifacts'),restore=captureFocus(container);let view=fileViews.get(container);
  if(!view||view.taskId!==task.id){view={taskId:task.id,groups:new Map(),rows:new Map(),featured:new Map()};fileViews.set(container,view);container.replaceChildren();}
  const currentPaths=new Set(task.files.map(file=>file.path));for(const path of view.rows.keys())if(!currentPaths.has(path))view.rows.delete(path);
  const groups=[['Файлы результата','outputs/',true],['Входные материалы','inputs/',false],['Рабочие и промежуточные файлы',null,false]],nodes=[];
  if(!task.files.length)nodes.push(empty('Файлы появятся здесь','Исполнители сохраняют результаты по ходу работы. Их можно скачать или посмотреть.'));
  for(const [label,prefix,open] of groups){
    const files=task.files.filter(f=>prefix?f.path.startsWith(prefix):!f.path.startsWith('outputs/')&&!f.path.startsWith('inputs/'));if(!files.length)continue;const key=prefix||'other';let group=view.groups.get(key);
    if(!group){const node=element('details',undefined,'file-group'),summary=element('summary'),content=element('div');node.open=open;summary.dataset.focusKey='file-group:'+task.id+':'+key;node.append(summary,content);group={node,summary,content,loaded:false,files:[]};view.groups.set(key,group);group.fill=()=>{group.loaded=true;const rows=group.files.map(file=>{let entry=view.rows.get(file.path);if(!entry||entry.size!==file.size){entry={size:file.size,node:fileRow(task,file,true)};view.rows.set(file.path,entry);entry.node.querySelector('a').dataset.focusKey='download:'+task.id+':'+file.path;const preview=entry.node.querySelector('.preview summary');if(preview)preview.dataset.focusKey='preview:'+task.id+':'+file.path;}return entry.node;});reconcileChildren(group.content,rows);};node.addEventListener('toggle',()=>{if(node.open)group.fill();});}
    group.files=files;group.summary.textContent=label+' · '+files.length;if(group.loaded||group.node.open)group.fill();nodes.push(group.node);
  }
  reconcileChildren(container,nodes);restore();
  const outputs=task.files.filter(f=>f.path.startsWith('outputs/')&&f.path!=='outputs/final-answer.md');$('featured-files').classList.toggle('hidden',!outputs.length);
  const featuredRestore=captureFocus($('output-links')),featured=outputs.slice(0,3).map(file=>{let entry=view.featured.get(file.path);if(!entry||entry.size!==file.size){entry={size:file.size,node:fileRow(task,file)};entry.node.querySelector('a').dataset.focusKey='featured:'+task.id+':'+file.path;view.featured.set(file.path,entry);}return entry.node;});reconcileChildren($('output-links'),featured);featuredRestore();
}
function safeText(value){
  return String(value??'').replace(/\b(Bearer\s+)[A-Za-z0-9._~+\/-]+=*/gi,'$1[скрыто]').replace(/((?:api[_ -]?key|access[_ -]?token|secret|password|пароль|ключ)\s*[:=]\s*["']?)[^\s"',;]+/gi,'$1[скрыто]').replace(/\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{15,})\b/g,'[скрыто]');
}
function acceptanceSummary(task){
  const statuses={checking:['Проверка приёмки','Проверка выполняется. Итог приёмки ещё не подтверждён.'],passed:['Приёмка подтверждена','Проверка указанных критериев и файлов завершена успешно.'],failed:['Приёмка не пройдена','Проверка завершилась ошибкой. Изучи причину и доказательства ниже.'],stale:['Приёмка устарела','Проверенные файлы изменились. Требуется новая проверка текущего результата.']};
  const status=task.acceptance?.status;
  if(Object.hasOwn(statuses,status)){const [label,description]=statuses[status];return {state:status,label,description};}
  return {state:'unverified',label:'Приёмка не подтверждена',description:task.state==='completed'?'Исполнение завершено. Независимое подтверждение критериев приёмки не зарегистрировано. Итог агента и успешные команды доступны для просмотра.':'Подтверждённых данных о приёмке пока нет. Счётчики агентов, файлов и команд отражают работу, а не готовность результата.'};
}
function renderAcceptance(task){
  const summary=acceptanceSummary(task),a=task.acceptance||{};
  $('acceptance-status').textContent=summary.label;$('acceptance-status').dataset.state=summary.state;
  $('acceptance-text').textContent=safeText([summary.description,a.summary].filter(Boolean).join(' '));textNotice('acceptance-reason',a.reason);
  const criteria=Array.isArray(a.criteria)?a.criteria:[],artifacts=Array.isArray(a.artifacts)?a.artifacts:[];
  const signature=JSON.stringify([task.id,a]);
  if(renderSignatures.get($('acceptance-evidence'))!==signature){
    if($('acceptance-evidence').dataset.task!==task.id)$('acceptance-evidence').open=false;
    $('acceptance-evidence').dataset.task=task.id;renderSignatures.set($('acceptance-evidence'),signature);
    $('acceptance-criteria').replaceChildren(...criteria.map(c=>element('li',c)));
    $('acceptance-evidence').classList.toggle('hidden',!task.acceptance);
    $('acceptance-scope').textContent='Объём проверки: '+artifacts.length+' файлов · критериев: '+criteria.length;
    const fields=[['Источник',a.source==='external_audit'?'Внешняя проверка (external_audit)':a.source==='runtime_validation'?'Проверка runtime (runtime_validation)':a.source||'Не указан'],['Проверено',a.checked_at?date(a.checked_at):'Пока не завершено'],['Workflow',a.flow||'Не указан'],['Задание проверки',a.job_id||'Не зарегистрировано'],['Код завершения',a.exit_code??'Не получен']];
    $('acceptance-metadata').replaceChildren(...fields.flatMap(([name,value])=>[element('dt',name+':'),element('dd',value)]));
    $('acceptance-command').textContent=safeText(a.command||'Команда не зарегистрирована.');
    $('acceptance-artifacts').replaceChildren(...artifacts.map(file=>element('li',file.path+' · SHA-256: '+file.sha256)));
  }
  const hasJob=Boolean(a.job_id&&(task.codex_jobs||[]).some(job=>job.id===a.job_id));
  $('acceptance-job').classList.toggle('hidden',!hasJob);$('acceptance-job').textContent=safeText('Открыть задание '+(a.job_id||''));
  textNotice('acceptance-job-note',a.job_id&&!hasJob?'Задание '+a.job_id+' отсутствует в доступном списке команд.':'');
}
function recoverySummary(task){
  const r=task.recovery||{status:'disabled',attempt:0,max_attempts:3};
  const labels={idle:'Автовосстановление включено',scheduled:'Ожидание восстановления',continued:'Продолжение создано',exhausted:'Автоматические попытки исчерпаны',blocked:'Автовосстановление невозможно',disabled:'Автовосстановление отключено'};
  return {data:r,label:labels[r.status]||'Состояние восстановления неизвестно',scheduled:r.status==='scheduled'};
}
function renderRecovery(task){
  const {data:r,label,scheduled}=recoverySummary(task),section=$('recovery-section');section.classList.remove('hidden');section.dataset.state=r.status;
  $('recovery-status').textContent=label;
  const used=(r.attempt||0)+(r.status==='continued'?1:0),attempt=scheduled?used+1:used,limit=r.max_attempts??3,remaining=Math.max(0,limit-used);
  const seconds=Math.max(0,Math.ceil((Date.parse(r.retry_at)-Date.now())/1000));
  $('recovery-timing').textContent=scheduled?'Попытка '+attempt+' из '+limit+' · '+(Number.isFinite(seconds)&&seconds>0?'через '+seconds+' с · '+date(r.retry_at):'Ожидаем запуска')+'. Осталось попыток: '+remaining+'.':r.status==='disabled'?(task.state==='completed'?'Исторический завершённый запуск. Автовосстановление включено в новых задачах.':'Автоматическое продолжение для этого запуска не запланировано.'):'Использовано попыток: '+attempt+' из '+limit+'. Осталось: '+remaining+'.';
  textNotice('recovery-reason',r.reason);
  const calls=Math.max(0,task.llm_limit-task.llm_calls),agents=Math.max(0,task.agent_limit-task.agents.filter(a=>a.parent).length),commands=task.command_budget?.remaining;
  $('recovery-budget').textContent=r.status==='disabled'?'':'Остаток бюджета запуска — GLM: '+calls+' · агентов: '+agents+' · команд: '+(commands??'нет данных')+'. Автовосстановление не пополняет бюджет.';
}
function groupGoalRuns(items){
  const nodes=Object.fromEntries(items.map(t=>[t.id,t])),groups=new Map();
  for(const task of items){let origin=task,seen=new Set();while(origin.resumed_from&&nodes[origin.resumed_from]&&!seen.has(origin.id)){seen.add(origin.id);origin=nodes[origin.resumed_from];}const key=seen.has(origin.id)?[...seen].sort()[0]:origin.id;if(!groups.has(key))groups.set(key,{id:key,origin,runs:[]});groups.get(key).runs.push(task);}
  return [...groups.values()].map(group=>({...group,current:group.runs.find(t=>!t.resumed_as||!nodes[t.resumed_as])||group.runs[0]}));
}
function buildFlowHierarchy(flows){
  const nodes=Object.fromEntries(flows.map(a=>[a.id,a])),children=Object.fromEntries(flows.map(a=>[a.id,[]])),roots=[];
  for(const agent of flows){if(agent.parent&&nodes[agent.parent]&&agent.parent!==agent.id)children[agent.parent].push(agent.id);else roots.push(agent.id);}
  const visited=new Set();function walk(id){if(visited.has(id))return;visited.add(id);for(const child of children[id])walk(child);}roots.forEach(walk);for(const agent of flows){if(!visited.has(agent.id)){roots.push(agent.id);walk(agent.id);}}
  return {nodes,children,roots};
}
function renderList(){
  const query=$('task-search').value.trim().toLocaleLowerCase(),goals=groupGoalRuns(tasks);
  const signature=JSON.stringify([tasks.map(t=>[t.id,t.state,t.prompt,t.resumed_from,t.resumed_as,t.recovery]),selected,query,filter]);if(signature===listSignature)return;listSignature=signature;
  const active=t=>activeStates.includes(t.state)||t.recovery?.status==='scheduled',attention=t=>['failed','stopped','suspended'].includes(t.state)&&t.recovery?.status!=='scheduled';
  $('total-count').textContent=goals.length;$('active-count').textContent=goals.filter(g=>active(g.current)).length;$('attention-count').textContent=goals.filter(g=>attention(g.current)).length;
  const rows=goals.filter(g=>(filter!=='active'||active(g.current))&&(filter!=='attention'||attention(g.current))&&safeText(g.origin.prompt).toLocaleLowerCase().includes(query));
  const container=$('task-list'),existing=new Map([...container.querySelectorAll('[data-goal]')].map(b=>[b.dataset.goal,b])),nodes=[];
  if(!rows.length)nodes.push(element('p',query?'Таких целей нет. Попробуй другой запрос.':filter==='attention'?'Нет целей, требующих внимания.':filter==='active'?'Сейчас нет активных целей.':'Здесь появятся цели и результаты.','empty-hint'));
  for(const goal of rows){const task=goal.current,on=goal.runs.some(t=>t.id===selected);let b=existing.get(goal.id);if(!b){b=element('button');b.dataset.goal=goal.id;const meta=element('small');meta.append(element('span'),element('span'));b.append(element('i'),element('b'),meta);b.addEventListener('click',()=>showTask(b.dataset.task));}b.dataset.task=task.id;b.className='task-item'+(on?' selected':'');if(on)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');b.querySelector('i').className='dot '+(task.recovery?.status==='scheduled'?'queued':task.state);b.querySelector('b').textContent=shortTitle(safeText(goal.origin.prompt),100);const meta=b.querySelector('small');meta.children[0].textContent=task.recovery?.status==='scheduled'?recoverySummary(task).label:stateNames[task.state]||task.state;meta.children[1].textContent=goal.runs.length+' зап.';nodes.push(b);}
  reconcileChildren(container,nodes);
}
function flowState(agent){return ['completed','failed','stopped','terminated','stalled'].includes(agent.state)?agent.state:agent.execution_state||agent.state;}
function flowLabel(agent){return agent.title||(agent.assignment?shortTitle(agent.assignment,100):'')||((roles[agent.agent]||[agent.agent||'Исполнитель'])[0]+' · '+agent.id.slice(0,8));}
function treeSnapshot(){
  const focused=document.activeElement?.dataset.focusKey;
  return ()=>{if(focused){const target=[...document.querySelectorAll('[data-focus-key]')].find(n=>n.dataset.focusKey===focused);target?.focus({preventScroll:true});}};
}
function renderHierarchy(task,container,compact=false){
  const restore=treeSnapshot(),tree=buildFlowHierarchy(task.agents),scope=container.id,query=compact?'':$('team-search').value.toLocaleLowerCase().trim(),problems=!compact&&$('team-problems').checked;
  const matches=a=>(!query||safeText([flowLabel(a),a.assignment,a.id,a.agent,(roles[a.agent]||[])[0]].filter(Boolean).join(' ')).toLocaleLowerCase().includes(query))&&(!problems||['failed','stopped','suspended'].includes(flowState(a)));
  function descendants(id,seen=new Set()){if(seen.has(id))return [];seen.add(id);return [id,...tree.children[id].flatMap(c=>descendants(c,seen))];}
  function branch(id,ancestors=new Set()){
    if(ancestors.has(id))return null;const chain=new Set([...ancestors,id]),agent=tree.nodes[id],all=descendants(id),visible=all.some(x=>matches(tree.nodes[x]));if(!visible)return null;
    const children=tree.children[id].filter(x=>!chain.has(x));const wrap=element('details',undefined,'flow-group');wrap.dataset.flow=id;const key=task.id+':'+scope+':'+id;wrap.open=expandedFlows.has(key)||(Boolean(query)||problems);
    const summary=element('summary');summary.dataset.focusKey=scope+':'+id;const arrow=element('span',undefined,'tree-chevron');arrow.append(icon('chevron'));const title=element('span',undefined,'flow-title');title.append(element('b',flowLabel(agent)),element('small',children.length?`${all.length-1} дочерних · ${all.filter(x=>tree.nodes[x].state==='completed').length} workflow завершено`:'GLM · отдельный исполнитель'));summary.append(arrow,title,badge(flowState(agent)));wrap.append(summary);
    let built=false;const content=element('div',undefined,'flow-children');function fill(){if(built)return;built=true;const info=element('button','Поручение и события','text-button flow-detail-button');info.dataset.focusKey=scope+':detail:'+id;info.addEventListener('click',()=>openAgent(task,id,info.dataset.focusKey));content.append(info);if(agent.assignment)content.append(element('p',shortTitle(safeText(agent.assignment),200),'muted'));for(const child of children){const node=branch(child,chain);if(node)content.append(node);}wrap.append(content);}
    if(wrap.open)fill();wrap.addEventListener('toggle',()=>{if(wrap.open){expandedFlows.add(key);fill();}else expandedFlows.delete(key);});return wrap;
  }
  container.replaceChildren();let roots=tree.roots;
  if(compact&&roots.length===1&&tree.children[roots[0]].length)roots=tree.children[roots[0]];
  if(!task.agents.length){container.append(empty('Команда ещё не сформирована','Координатор добавит направления по мере работы.','activity'));return;}
  for(const root of roots){const row=branch(root);if(row)container.append(row);}
  if(!container.children.length)container.append(element('p','Нет исполнителей, соответствующих фильтру.','empty-hint'));restore();
}
function openAgent(task,id,returnKey){
  selectedAgent=id;agentReturnKey=returnKey||agentReturnKey;renderAgentDetails(task);$('agent-detail').classList.remove('hidden');$('close-agent-detail').focus();
}
function closeAgent(){selectedAgent=null;$('agent-detail').classList.add('hidden');[...document.querySelectorAll('[data-focus-key]')].find(n=>n.dataset.focusKey===agentReturnKey)?.focus();}
function commandOutcome(job){
  let exitCode=null;
  if(job.result){try{const code=JSON.parse(job.result).exit_code;if(typeof code==='number'&&Number.isInteger(code))exitCode=code;}catch{}}
  if(exitCode!==null&&exitCode!==0)return {state:'failed',label:'Ошибка команды',detail:'Код завершения: '+exitCode};
  if(exitCode===0)return {state:'completed',label:'Код 0',detail:'Код завершения: 0'};
  return {state:job.state==='completed'?'unverified':job.state,label:job.state==='completed'?'Код не подтверждён':stateNames[job.state]||job.state,detail:''};
}
function renderJobList(task,container,list,kind){
  let view=jobListViews.get(container);
  if(!view||view.taskId!==task.id){view={taskId:task.id,limit:20,signatures:new WeakMap()};jobListViews.set(container,view);container.replaceChildren();}
  view.task=task;view.list=list;
  const focused=document.activeElement,focusKey=container.contains(focused)?focused?.dataset.focusKey:null;
  const existing=new Map([...container.querySelectorAll('details[data-job]')].map(row=>[row.dataset.job,row]));
  const recent=list.map((job,index)=>({job,index})).sort((a,b)=>String(b.job.created).localeCompare(String(a.job.created))||b.index-a.index);
  // Keep an opened or focused older row visible when a new command arrives.
  const shown=recent.filter(({job},index)=>index<view.limit||job.id===view.evidenceId||existing.get(job.id)?.open||existing.get(job.id)?.contains(focused));
  const nodes=[];
  if(!list.length)nodes.push(element('p','Команды ещё не запускались.','empty-hint'));
  else nodes.push(element('p','Показано '+shown.length+' из '+list.length+' · сначала последние','muted'));
  for(const {job,index} of shown){
    let row=existing.get(job.id);
    if(!row){row=element('details',undefined,'job-row');row.dataset.job=job.id;const summary=element('summary'),label=element('span');summary.dataset.focusKey=container.id+':job:'+job.id;label.append(element('b'),element('small'));summary.append(icon('code'),label,badge(job.state));row.append(summary);}
    const signature=JSON.stringify([job,index]);
    if(view.signatures.get(row)!==signature){
      view.signatures.set(row,signature);row.querySelector('b').textContent=kind+' '+(index+1);
      const outcome=job.kind==='command'?commandOutcome(job):{state:job.state,label:stateNames[job.state]||job.state,detail:''};
      row.querySelector('small').textContent=safeText([outcome.detail,job.error].filter(Boolean).join(' · ')||date(job.created));
      const status=row.querySelector('.status');status.dataset.state=outcome.state;status.textContent=outcome.label;
      let output=row.querySelector('pre');if(job.result||job.error){if(!output){output=element('pre');row.append(output);}output.textContent=safeText(job.result||job.error);}else output?.remove();
    }
    nodes.push(row);
  }
  if(view.limit<list.length){const more=element('button','Показать ещё '+Math.min(20,list.length-view.limit),'text-button');more.dataset.focusKey=container.id+':more';more.addEventListener('click',()=>{view.limit+=20;renderJobList(view.task,container,view.list,kind);});nodes.push(more);}
  container.replaceChildren(...nodes);
  if(focusKey){const target=[...container.querySelectorAll('[data-focus-key]')].find(node=>node.dataset.focusKey===focusKey)||container.querySelector('details:last-of-type summary');target?.focus({preventScroll:true});}
}
function renderTeam(task){
  renderHierarchy(task,$('agents'));renderHierarchy(task,$('overview-groups'),true);
  const allJobs=task.codex_jobs||[],commands=allJobs.filter(j=>j.kind==='command'),jobs=allJobs.filter(j=>j.kind!=='command');
  renderJobList(task,$('command-jobs'),commands,'Команда');renderJobList(task,$('codex-jobs'),jobs,'Историческое задание Codex');
  $('legacy-codex-section').classList.toggle('hidden',!jobs.length);$('codex-policy').textContent='Исторические запуски. Новые задачи и продолжения используют только GLM.';renderAgentDetails(task);
}
function renderBudget(task){
  const used=(task.codex_jobs||[]).filter(j=>j.kind==='command').length,b=task.command_budget;
  $('call-stat').textContent=task.llm_calls.toLocaleString('ru-RU')+' / '+task.llm_limit.toLocaleString('ru-RU');$('agent-stat').textContent=task.agents.filter(a=>a.parent).length+' / '+task.agent_limit;
  $('command-stat').textContent=b?b.used+' / '+b.limit:used+' / '+(task.command_limit??100);$('work-stat').textContent=b?String(b.work_remaining):'Нет данных';$('review-stat').textContent=b?String(b.review_remaining):'Нет данных';
  $('budget-policy').textContent=task.work_window?'Окно исполнителей: '+task.work_window+' на цель':'Окно не задано';
  $('budget-note').textContent=b?'Осталось команд: '+b.remaining+'. Резерв проверки: '+b.reserve+' из общего лимита. До 8 запросов GLM одновременно.':'Расход команд — по сохранённым заданиям. Сервер не передал остаток и резерв бюджета.';
  $('budget-note').classList.toggle('budget-exhausted',Boolean(b&&b.work_remaining===0));
}
function renderTask(task){
  currentTask=task;$('loading').classList.add('hidden');$('task-detail').classList.remove('hidden');$('updated-at').textContent='Обновлено '+new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  const signature=JSON.stringify(task);renderRunHistory(task);renderRecovery(task);if(signature===detailSignature)return;detailSignature=signature;
  $('page-label').textContent='Цель';$('task-title').textContent=shortTitle(safeText(task.prompt));$('task-date').textContent=date(task.created);$('task-status').textContent=stateNames[task.state]||task.state;$('task-status').dataset.state=task.state;
  renderAcceptance(task);
  $('files-count').textContent=task.files.length;$('event-count').textContent=task.events.length;$('team-count').textContent=task.agents.length;$('stop-task').classList.toggle('hidden',!activeStates.includes(task.state)&&task.recovery?.status!=='scheduled');$('resume-task').classList.toggle('hidden',!['failed','stopped'].includes(task.state)||task.recovery?.status==='scheduled');$('resume-label').textContent=task.resumed_as?'Открыть продолжение':'Продолжить';$('resume-parent').classList.toggle('hidden',!task.resumed_from);if(task.resumed_from){$('resume-parent').href='#'+task.resumed_from;$('resume-parent').textContent='Предыдущий запуск';}
  textNotice('task-error',safeText(task.error));$('original-prompt').textContent=safeText(task.prompt);
  const done=task.state==='completed',counts={};for(const agent of task.agents){const key=flowState(agent);counts[key]=(counts[key]||0)+1;}
  $('flow-counts').textContent=Object.entries(counts).map(([key,count])=>(stateNames[key]||key)+': '+count).join(' · ')||'Исполнители пока не созданы.';
  const hasAction=task.events.some(e=>['tool','file','search','plan','command'].includes(e.kind));
  $('progress-title').textContent={preparing:'Подготовка материалов',queued:'Ожидаем координатора',running:hasAction?'Выполнение задачи':'Ожидаем действия координатора',completed:'Исполнение завершено',stopped:'Запуск остановлен',failed:'Работа прервалась',suspended:'Запуск приостановлен'}[task.state]||stateNames[task.state];
  const useful=task.events.findLast(e=>['file','plan','command'].includes(e.kind));
  $('progress-text').textContent=done?'Итог и сохранённые файлы доступны ниже. Статус приёмки указан отдельно.':['failed','stopped'].includes(task.state)?'Сохранённые файлы доступны. «Продолжить» создаст связанный запуск с контекстом и новыми лимитами.':useful?'Последнее событие результата: '+(eventNames[useful.kind]||useful.kind).toLocaleLowerCase()+' · '+date(useful.created)+'.':task.llm_calls?'Обращения к модели зарегистрированы. Подтверждённых событий результата пока нет.':'Файлы сохранены; подтверждённого действия модели пока нет.';
  const plan=task.events.findLast(e=>e.kind==='plan');$('plan-section').classList.toggle('hidden',!plan);if(plan){try{const steps=JSON.parse(plan.content);if(!Array.isArray(steps))throw new Error();$('plan').replaceChildren(...steps.map(s=>element('li',typeof s==='string'?s:JSON.stringify(s))));}catch{$('plan-section').classList.add('hidden');}}
  $('result-section').classList.toggle('hidden',!task.result);$('journal-note').textContent=task.events.length>=250?'Показаны последние '+task.events.length+' событий. Ранние события не входят в этот ответ API.':'Показано событий: '+task.events.length+'. Полные сообщения раскрываются отдельно.';
  renderBudget(task);renderMarkdown(task);renderEvents(task);renderTeam(task);renderFiles(task);
}
async function refresh(){if(busy)return;busy=true;try{const [health,list]=await Promise.all([api('/api/health'),api('/api/tasks')]);healthy=health.ready;tasks=list;$('connection').replaceChildren(element('i',undefined,'dot'+(health.ready?' ready':'')),element('span',health.ready?'Локальный сервис доступен':'Dapr запускается'));$('glm-dot').classList.remove('ready');$('glm-dot').parentElement.title='GLM настроен. Доступность модели определяется по запросам задачи.';$('codex-dot').classList.toggle('ready',Boolean(health.codex?.ready));$('codex-dot').parentElement.title=health.codex?.ready?'Локальные команды доступны, без запросов GPT':'Исполнитель команд недоступен: запустите start.command';$('submit').disabled=!healthy||submitting;textNotice('notice',health.error||'');renderList();let id=selected;const seen=new Set();
  while(id&&!seen.has(id)){
    seen.add(id);const task=await api('/api/tasks/'+id);if(selected!==id)break;
    if(followContinuation===null)followContinuation=!task.resumed_as&&!task.recovery?.next_task_id;
    const next=task.recovery?.status==='continued'?task.recovery.next_task_id:null;
    if(followContinuation&&next&&next!==id&&!seen.has(next)){
      closeAgent();selected=next;currentTask=null;detailSignature='';filesSignature='';history.replaceState(null,'','#'+next);renderList();id=next;continue;
    }
    lastUpdated=new Date();renderTask(task);break;
  }}catch(error){healthy=false;$('connection').replaceChildren(element('i',undefined,'dot'),element('span','Нет связи с API'));if(lastUpdated)$('updated-at').textContent='Данные устарели · '+lastUpdated.toLocaleTimeString('ru-RU');$('submit').disabled=true;textNotice('notice','Нет соединения с системой. '+error.message+' Если сервис остановлен, запусти start.command.');}finally{busy=false;}}
function updateAttachments(){const box=$('attachments');box.replaceChildren();attached.forEach((file,i)=>{const chip=element('div',undefined,'attachment');const b=element('button');b.type='button';b.setAttribute('aria-label','Убрать '+file.name);b.append(icon('close'));b.addEventListener('click',()=>{attached.splice(i,1);updateAttachments();});chip.append(icon('file'),element('span',file.name+' · '+size(file.size)),b);box.append(chip);});$('file-count').textContent=attached.length?'Файлов: '+attached.length+' / 10':'Текст и PDF · до 10 MiB';}
function addFiles(files){const merged=[...attached];for(const file of files){if(file.size>10*1024*1024){textNotice('form-error','Файл «'+file.name+'» больше 10 MiB.');return;}if(!merged.some(f=>f.name===file.name))merged.push(file);}if(merged.length>10){textNotice('form-error','Можно приложить до 10 файлов.');return;}attached=merged;updateAttachments();textNotice('form-error','');}
function updateChars(){$('char-count').textContent=$('prompt').value.length.toLocaleString('ru-RU')+' / 30 000';}
$('new-task').addEventListener('click',()=>showTask(null));$('home-link').addEventListener('click',()=>showTask(null));$('menu-toggle').addEventListener('click',()=>setSidebar(!$('sidebar').classList.contains('open')));$('sidebar-backdrop').addEventListener('click',()=>{setSidebar(false);$('menu-toggle').focus();});$('task-search').addEventListener('input',renderList);
document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{filter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));renderList();}));
document.querySelectorAll('[data-tab]').forEach(b=>{b.addEventListener('click',()=>setTab(b.dataset.tab));b.addEventListener('keydown',e=>{const names=['overview','team','files','activity'];const i=names.indexOf(activeTab);if(!['ArrowRight','ArrowLeft','Home','End'].includes(e.key))return;e.preventDefault();setTab(names[e.key==='Home'?0:e.key==='End'?3:(i+(e.key==='ArrowRight'?1:3))%4],true);});});
$('show-files').addEventListener('click',()=>setTab('files',true));$('files').addEventListener('change',e=>{addFiles(e.target.files);e.target.value='';});$('prompt').addEventListener('input',updateChars);
for(const event of ['dragenter','dragover'])$('drop-zone').addEventListener(event,e=>{e.preventDefault();$('drop-zone').classList.add('dragging');});$('drop-zone').addEventListener('dragleave',e=>{if(!$('drop-zone').contains(e.relatedTarget))$('drop-zone').classList.remove('dragging');});$('drop-zone').addEventListener('drop',e=>{e.preventDefault();$('drop-zone').classList.remove('dragging');addFiles(e.dataTransfer.files);});
document.querySelectorAll('[data-example]').forEach(b=>b.addEventListener('click',()=>{$('prompt').value=b.dataset.example;updateChars();$('prompt').focus();}));
$('task-form').addEventListener('submit',async e=>{e.preventDefault();if(submitting)return;submitting=true;$('submit').disabled=true;$('submit').firstElementChild.textContent='Запускаем…';textNotice('form-error','');try{const data=new FormData(e.target);const criteria=$('criteria').value.trim();if(criteria)data.set('prompt',data.get('prompt')+'\n\nКритерии приёмки:\n'+criteria);if(String(data.get('prompt')).length>30000)throw new Error('Цель вместе с критериями должна занимать не более 30 000 символов.');data.delete('allow_codex');data.delete('files');attached.forEach(f=>data.append('files',f));const task=await api('/api/tasks',{method:'POST',body:data});e.target.reset();syncBudgetFields();attached=[];updateAttachments();updateChars();showTask(task.id);}catch(error){textNotice('form-error',error.message);}finally{submitting=false;$('submit').disabled=!healthy;$('submit').firstElementChild.textContent='Запустить задачу';}});
$('resume-task').addEventListener('click',async()=>{const id=selected;const known=currentTask?.resumed_as;if(known){showTask(known);return;}$('resume-task').disabled=true;try{const next=await api('/api/tasks/'+id+'/resume',{method:'POST'});detailSignature='';showTask(next.id);await refresh();toast('Продолжение создано. Файлы сохранены.');}catch(error){textNotice('notice',error.message);}finally{$('resume-task').disabled=false;}});
$('resume-parent').addEventListener('click',event=>{event.preventDefault();if(currentTask?.resumed_from)showTask(currentTask.resumed_from,true,false);});
$('stop-task').addEventListener('click',async()=>{const id=selected;$('stop-task').disabled=true;try{await api('/api/tasks/'+id+'/stop',{method:'POST'});detailSignature='';await refresh();toast('Новые действия заблокированы');}catch(error){textNotice('notice',error.message);}finally{$('stop-task').disabled=false;}});
$('reuse-task').addEventListener('click',()=>{if(!currentTask)return;const task=currentTask;showTask(null);$('prompt').value=task.prompt;$('criteria').value='';$('task-form').elements.agent_limit.value=task.agent_limit;$('task-form').elements.llm_limit.value=task.llm_limit;$('allow-codex').checked=false;for(const field of ['command_limit','command_reserve','work_window'])if(task[field]!==undefined)$('task-form').elements[field].value=field==='work_window'&&!task[field]?16:task[field];syncBudgetFields();updateChars();toast('Текст перенесён. Вложения при необходимости добавь заново.');});
$('copy-result').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(currentTask?.result||'');toast('Результат скопирован');}catch{toast('Копирование недоступно. Выдели текст результата.');}});
window.addEventListener('popstate',()=>showTask(location.hash.slice(1)||null,false,false));window.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();showTask(null);}if(e.key==='Escape'&&selectedAgent)closeAgent();if(e.key==='Escape'&&$('sidebar').classList.contains('open')){setSidebar(false);$('menu-toggle').focus();}});
function syncBudgetFields(){const form=$('task-form');form.elements.command_reserve.max=Math.max(0,Number(form.elements.command_limit.value)-1);}
$('task-form').elements.command_limit.addEventListener('input',syncBudgetFields);
$('show-team').addEventListener('click',()=>setTab('team',true));
$('acceptance-job').addEventListener('click',()=>{
  const id=currentTask?.acceptance?.job_id,job=currentTask?.codex_jobs?.find(j=>j.id===id);if(!job)return;
  const container=$(job.kind==='command'?'command-jobs':'codex-jobs');setTab('team');
  const view=jobListViews.get(container);if(view)view.evidenceId=id;renderTeam(currentTask);
  const row=[...container.querySelectorAll('[data-job]')].find(node=>node.dataset.job===id);if(row){row.open=true;row.querySelector('summary').focus();}
});
$('team-search').addEventListener('input',()=>{if(currentTask)renderHierarchy(currentTask,$('agents'));});
$('team-problems').addEventListener('change',()=>{if(currentTask)renderHierarchy(currentTask,$('agents'));});
$('close-agent-detail').addEventListener('click',closeAgent);
showTask(selected,false);setInterval(refresh,2000);
