def test_real_sdk_delegation_preserves_trusted_parent():
    from app.agents import ParentLinkedContext
    from dapr_agents.tool.workflow.agent_tool import _schedule_agent_workflow

    class Recorder:
        instance_id = 'trusted-parent'
        is_replaying = False

        def call_child_workflow(self, **kwargs):
            return kwargs

    result = _schedule_agent_workflow(ParentLinkedContext(Recorder()), task='Research',
        agent_name='researcher', _source_agent='coordinator', _child_instance_id='child')
    assert result['input']['workflow_instance_id'] == 'trusted-parent'
    assert result['input']['task'] == 'Research'
    assert result['instance_id'] == 'child'


def test_codex_activity_identity_survives_replay_without_cross_turn_collision(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from dapr_agents import DurableAgent
    from app.agents import LocalAgent
    from app.codex_jobs import CodexJobs
    from app.store import TaskStore
    from app.tools import tool_call_context
    store = TaskStore(tmp_path)
    jobs = CodexJobs(store)
    task = store.create('Code', 3, 10, allow_codex=True)
    store.update(task['id'], 'running')

    @contextmanager
    def scope(_):
        yield task['id']

    def execute(_self, ctx, payload):
        return jobs.enqueue(task['id'], task['id'], tool_call_context.get(), payload['prompt'])

    monkeypatch.setattr(DurableAgent, 'run_tool', execute)
    agent = object.__new__(LocalAgent)
    object.__setattr__(agent, 'task_scope', scope)
    object.__setattr__(agent, 'local_store', store)
    object.__setattr__(agent, 'name', 'coordinator')
    payload = {'instance_id':task['id'], 'prompt':'First task',
               'tool_call': {'id':'call_0', 'function':{'name':'run_codex'}}}
    first = LocalAgent.run_tool(agent, SimpleNamespace(task_id=10), payload)
    replay = LocalAgent.run_tool(agent, SimpleNamespace(task_id=10), payload)
    second = LocalAgent.run_tool(agent, SimpleNamespace(task_id=20), {**payload, 'prompt':'Second task'})
    assert first['id'] == replay['id']
    assert second['id'] != first['id']
    assert len(jobs.list(task['id'])) == 2


def test_hierarchical_request_requires_delegation_and_bounds_depth(tmp_path):
    from app.agents import swarm_request_options
    from app.store import TaskStore
    store = TaskStore(tmp_path)
    task = store.create('Swarm', 10, 50)
    with store.connect() as db:
        db.execute("UPDATE flows SET agent='swarm_coordinator' WHERE id=?", (task['id'],))
    tools = [{'type':'function','function':{'name':n}} for n in ['swarm_builder','swarm_lead','read_file','run_command','run_codex']]
    options, required = swarm_request_options(store, task['id'], {'tools':tools})
    assert required and options['tool_choice'] == 'required'
    assert options['reasoning_effort'] == 'low'
    assert {t['function']['name'] for t in options['tools']} == {'swarm_builder','swarm_lead','read_file'}
    store.register_flow('one',task['id'],'swarm_lead')
    options, required = swarm_request_options(store, task['id'], {'tools':tools})
    assert not required
    assert not {'run_command','run_codex'} & {t['function']['name'] for t in options['tools']}
    parent = 'one'
    for i in range(2,6):
        child = str(i);store.register_flow(child,parent,'swarm_builder');parent=child
    assert store.flow_depth(parent) == 5
    options, required = swarm_request_options(store,parent,{'tools':tools})
    assert not required and not any(t['function']['name'].startswith('swarm_') for t in options['tools'])
    assert options['reasoning_effort'] == 'low'
    import pytest
    with pytest.raises(RuntimeError,match='глубин'):
        store.register_flow('too-deep',parent,'swarm_builder')
    assert store.root_for(parent)==task['id']


def test_legacy_requests_are_unchanged(tmp_path):
    from app.agents import swarm_request_options
    from app.store import TaskStore
    store=TaskStore(tmp_path);task=store.create('Existing workflow',2,10)
    options={'tools':[{'type':'function','function':{'name':'run_command'}}]}
    assert swarm_request_options(store,task['id'],options)==(options,False)


def test_swarm_delegate_cannot_override_destination():
    import pytest
    from app.agents import swarm_delegate, ParentLinkedContext
    class Recorder:
        instance_id='trusted-parent'
        is_replaying=False
        def call_child_workflow(self, **kwargs):
            return kwargs
    tool=swarm_delegate('swarm_builder','Builder')
    for key,value in [('agent_name','coordinator'),('workflow_name','dapr.agents.coordinator.workflow'),('target_app_id','other')]:
        with pytest.raises(Exception):
            tool(ctx=ParentLinkedContext(Recorder()), task='Work', **{key:value})
    result=tool(ctx=ParentLinkedContext(Recorder()),task='Work')
    assert result['workflow']=='dapr.agents.swarm_builder.workflow'
    assert result['input']['workflow_instance_id']=='trusted-parent'


def test_leader_reserves_capacity_for_its_first_child(tmp_path):
    import pytest
    from app.agents import swarm_request_options
    from app.store import TaskStore
    store=TaskStore(tmp_path);task=store.create('Small budget',2,20)
    store.register_flow('lead',task['id'],'swarm_lead')
    with pytest.raises(RuntimeError):
        store.register_flow('competing-lead',task['id'],'swarm_lead')
    with pytest.raises(RuntimeError):
        store.register_flow('unrelated-leaf',task['id'],'swarm_builder')
    tools=[{'type':'function','function':{'name':n}} for n in ['swarm_lead','swarm_builder']]
    options,required=swarm_request_options(store,'lead',{'tools':tools})
    assert required and [t['function']['name'] for t in options['tools']]==['swarm_builder']
    store.register_flow('leaf','lead','swarm_builder')
    assert len(store.flows(task['id']))==3
