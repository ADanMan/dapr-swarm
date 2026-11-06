import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.store import TaskStore
from app.codex_jobs import CodexJobs


def task_store(tmp_path, **options):
    store = TaskStore(tmp_path)
    task = store.create('Bounded work', 300, 2000, **options)
    store.update(task['id'], 'running')
    return store, task['id']


def test_command_reserve_is_atomic_and_available_only_to_reviewers(tmp_path):
    store, root = task_store(tmp_path, command_limit=6, command_reserve=2)
    jobs = CodexJobs(store)
    store.register_flow('worker', root, 'swarm_analyst')
    store.register_flow('review', root, 'swarm_reviewer')
    def enqueue(i):
        try:
            jobs.enqueue(root, 'worker', f'work:{i}', 'true', kind='command')
            return True
        except RuntimeError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(enqueue, range(20))) == 4
    for i in range(2):
        jobs.enqueue(root, 'review', f'review:{i}', 'true', kind='command')
    with pytest.raises(RuntimeError):
        jobs.enqueue(root, 'review', 'over', 'true', kind='command')
    # Replay never consumes another slot, even at the hard limit.
    assert jobs.enqueue(root, 'review', 'review:0', 'true', kind='command')
    assert jobs.budget(root)['remaining'] == 0


def test_work_window_is_atomic_persistent_and_released_for_nested_children(tmp_path):
    store, root = task_store(tmp_path, work_window=2)
    for i in range(20):
        store.register_flow(f'leaf-{i}', root, 'swarm_analyst')
    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(store.admit_flow, [f'leaf-{i}' for i in range(20)]))
    assert sum(admitted) == 2
    holder = next(f'leaf-{i}' for i, admitted in enumerate(admitted) if admitted)
    reopened = TaskStore(tmp_path)
    assert reopened.admit_flow(holder)
    reopened.release_flow(holder, 'waiting_children')
    reopened.register_flow('grandchild', holder, 'swarm_builder')
    assert reopened.admit_flow('grandchild')
    assert not reopened.admit_flow(holder)
    reopened.flow_state('grandchild', 'completed')
    assert reopened.admit_flow(holder)
    reopened.stop(root)
    with pytest.raises(RuntimeError):
        reopened.admit_flow(holder)


def test_json_record_selection_and_invalid_json_write_preserves_existing(tmp_path):
    from app.tools import make_tools, task_context
    from dapr_agents.types.exceptions import ToolError
    store, root = task_store(tmp_path)
    workspace = store.workspace(root)
    (workspace/'inputs').mkdir()
    (workspace/'inputs/batch.json').write_text(json.dumps([{'case_id':'A','value':1},{'case_id':'B','value':2}]))
    tools = {t.name:t for t in make_tools(store)}
    token = task_context.set((root,root))
    try:
        assert json.loads(tools['read_file'](path='inputs/batch.json', json_key='case_id', json_value='B')) == {'case_id':'B','value':2}
        with pytest.raises(ToolError):
            tools['read_file'](path='inputs/batch.json', json_key='case_id', json_value='missing')
        tools['write_file'](path='outputs/result.json',content='{"ready":true}')
        with pytest.raises(ToolError):
            tools['write_file'](path='outputs/result.json',content='{"broken":')
        assert json.loads((workspace/'outputs/result.json').read_text()) == {'ready':True}
    finally:
        task_context.reset(token)


def test_new_resource_settings_survive_resume(tmp_path):
    from app.resume import continue_task
    store, root = task_store(tmp_path, command_limit=700, command_reserve=30, work_window=7)
    CodexJobs(store)
    store.stop(root)
    resumed = continue_task(store, root)
    assert (resumed['command_limit'],resumed['command_reserve'],resumed['work_window']) == (700,30,7)


def test_async_model_admission_yields_for_children_and_releases_after_error(tmp_path, monkeypatch):
    from dapr_agents import DurableAgent
    from app.agents import LocalAgent
    from contextlib import contextmanager
    import inspect
    assert inspect.iscoroutinefunction(LocalAgent.call_llm)
    store, root = task_store(tmp_path, work_window=1)
    store.register_flow('parent',root,'swarm_analyst')
    store.register_flow('waiting',root,'swarm_analyst')
    agent=object.__new__(LocalAgent)
    object.__setattr__(agent,'local_store',store)
    object.__setattr__(agent,'name','swarm_analyst')
    @contextmanager
    def scope(flow):
        yield root
    object.__setattr__(agent,'task_scope',scope)
    monkeypatch.setattr(DurableAgent,'call_llm',lambda *_:{'tool_calls':[{'function':{'name':'swarm_builder'}}]})
    assert store.admit_flow('parent')
    asyncio.run(LocalAgent.call_llm(agent,SimpleNamespace(),{'instance_id':'parent'}))
    assert store.admit_flow('waiting'), 'Parent holding admission would deadlock recursive children'
    store.release_flow('waiting','waiting_children')
    def broken(*_):
        raise RuntimeError('provider failure')
    monkeypatch.setattr(DurableAgent,'call_llm',broken)
    assert store.admit_flow('parent')
    with pytest.raises(RuntimeError,match='provider failure'):
        asyncio.run(LocalAgent.call_llm(agent,SimpleNamespace(),{'instance_id':'parent'}))
    assert store.admit_flow('waiting'), 'Failed activities must not leak admission'


def test_durable_admission_waits_on_timer_and_preserves_old_history_shape():
    from app.agents import ParentLinkedContext, model_turn_workflow
    class Context:
        instance_id='parent'
        def call_activity(self,name,**kwargs):return ('activity',name,kwargs)
        def call_child_workflow(self,name,**kwargs):return ('workflow',name,kwargs)
        def create_timer(self,delay):return ('timer',delay.total_seconds())
    ctx=Context();payload={'instance_id':'parent'}
    legacy=ParentLinkedContext(ctx).call_activity('agent.call_llm',input=payload)
    assert legacy[0]=='activity'
    bounded=ParentLinkedContext(ctx,True).call_activity('agent.call_llm',input=payload)
    assert bounded[:2]==('workflow','swarm_model_turn_v1')
    gate=model_turn_workflow(ctx,bounded[2]['input'])
    assert next(gate)[:2]==('activity','swarm_admit_v1')
    assert gate.send(False)==('timer',3)
    assert gate.send(None)[:2]==('activity','swarm_admit_v1')
    assert gate.send(True)[:2]==('activity','agent.call_llm')
    with pytest.raises(StopIteration) as done:gate.send({'content':'verified'})
    assert done.value.value=={'content':'verified'}


def test_stop_remains_visible_when_inflight_work_releases_or_finalizes(tmp_path):
    store,root=task_store(tmp_path,work_window=1)
    store.register_flow('leaf',root,'swarm_analyst')
    assert store.admit_flow('leaf')
    store.stop(root)
    store.flow_state('leaf','stopped')
    store.release_flow('leaf','waiting_children')
    store.flow_state('leaf','finishing')
    flow=next(x for x in store.flows(root) if x['id']=='leaf')
    assert (flow['state'],flow['execution_state'],flow['admitted'])==('stopped','stopped',0)


def test_initial_assignment_is_forwarded_without_changing_activity_shape():
    from app.agents import ParentLinkedContext
    class Context:
        instance_id='trusted'
        def call_activity(self,name,**kwargs):return (name,kwargs)
    ctx=ParentLinkedContext(Context(),True,assignment='Analyze C007 into outputs/cases/C007.json')
    name,args=ctx.call_activity('agent.record_initial_entry',input={'instance_id':'trusted'})
    assert name=='agent.record_initial_entry'
    assert args['input']['assignment'].startswith('Analyze C007')
