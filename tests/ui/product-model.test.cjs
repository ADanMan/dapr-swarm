const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/app.js','utf8');
function call(name, ...args) {
  const start = source.indexOf('function '+name+'(');
  assert.notEqual(start,-1, name+' exists');
  const end = source.indexOf('\nfunction ',start+1);
  const code = source.slice(start,end<0?undefined:end);
  return vm.runInNewContext(code+'\n'+name+'(...args)', {args});
}
test('tree preserves parentage, includes orphan rows, and bounds cyclic data',()=>{
 const flows=[{id:'r',parent:null},{id:'a',parent:'r'},{id:'b',parent:'a'},{id:'orphan',parent:'missing'},{id:'x',parent:'y'},{id:'y',parent:'x'}];
 const tree=call('buildFlowHierarchy',flows);
 assert.deepEqual(Array.from(tree.roots),['r','orphan','x']);
 assert.deepEqual(Array.from(tree.children.r),['a']);
 assert.deepEqual(Array.from(tree.children.a),['b']);
 assert.equal(tree.nodes.b.id,'b');
});
test('completed workflow and successful commands cannot certify result acceptance',()=>{
 const summary=call('acceptanceSummary',{state:'completed',result:'Проверено',codex_jobs:[{state:'completed',result:'{"exit_code":0}'}]});
 assert.equal(summary.state,'unverified');
 assert.match(summary.label,/не подтверждена/);
});
test('goals collect linked runs without losing independent goals',()=>{
 const groups=call('groupGoalRuns',[{id:'b',resumed_from:'a'},{id:'c'},{id:'a',resumed_as:'b'}]);
 assert.equal(groups.length,2);
 assert.equal(groups[0].runs.length,2);
 assert.equal(groups[0].current.id,'b');
});
test('presentation hides common secrets without altering ordinary assignment text',()=>{
 assert.equal(call('safeText','Проверить отчёт'), 'Проверить отчёт');
 assert.doesNotMatch(call('safeText','api_key=secret123 Authorization: Bearer abc.def.ghi'),/secret123|abc\.def\.ghi/);
});
