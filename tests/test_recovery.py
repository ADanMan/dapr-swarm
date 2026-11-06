"""Recovery must preserve work without refilling budgets or bypassing Stop."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.store import TaskStore
from app.codex_jobs import CodexJobs
from app.main import Engine, create_app
from fastapi.testclient import TestClient


def setup_task(tmp_path):
    store = TaskStore(tmp_path)
    queue = CodexJobs(store)
    task = store.create('Finish existing work', 10, 50, command_limit=20, command_reserve=5)
    store.update(task['id'], 'running')
    (store.workspace(task['id'])/'outputs').mkdir()
    (store.workspace(task['id'])/'outputs/kept.txt').write_text('completed work')
    return store, queue, task['id']


def model_failure(store, root, kind='length'):
    from app.recovery import record_model_failure
    marker = record_model_failure(store, root, root, kind, 'Temporary model failure')
    error = 'Wrapped durable failure: '+marker+': Temporary model failure'
    store.flow_state(root, 'failed')
    store.update(root, 'failed', error=error)
    return error


def test_new_tasks_enable_recovery_but_migration_does_not_enable_history(tmp_path):
    store, _, root = setup_task(tmp_path)
    assert store.get(root)['auto_recover'] == 1
    with store.connect() as db:
        db.execute('UPDATE tasks SET auto_recover=0 WHERE id=?', (root,))
    reloaded = TaskStore(tmp_path)
    assert reloaded.get(root)['auto_recover'] == 0


def test_recovery_survives_restart_and_preserves_files_and_remaining_budgets(tmp_path, monkeypatch):
    from app.recovery import RecoveryManager, recovery_view
    store, queue, root = setup_task(tmp_path)
    for _ in range(7): store.reserve_call(root)
    store.register_flow('builder', root, 'swarm_builder')
    store.register_flow('reviewer', root, 'swarm_reviewer')
    for n, flow in enumerate(['builder','builder','reviewer']):
        job=queue.enqueue(root,flow,f'call:{n}','true',kind='command')
        queue.finish(job['id'],'completed',json.dumps({'exit_code':0}))
    error=model_failure(store,root)
    monkeypatch.setattr('app.recovery.time.time', lambda:1000)
    RecoveryManager(store).schedule(root,error)
    assert recovery_view(store.get(root))['status']=='scheduled'
    RecoveryManager(TaskStore(tmp_path)).process()
    assert store.get(root)['resumed_as'] is None
    monkeypatch.setattr('app.recovery.time.time', lambda:1015)
    RecoveryManager(TaskStore(tmp_path)).process()
    child=store.get(store.get(root)['resumed_as'])
    assert child['resumed_from']==root and child['recovery_attempt']==1
    assert (child['agent_limit'],child['llm_limit'],child['command_limit'],child['command_reserve'])==(8,43,17,4)
    assert (store.workspace(child['id'])/'outputs/kept.txt').read_text()=='completed work'
    assert store.get(root)['state']=='failed'
    assert recovery_view(store.get(root))['next_task_id']==child['id']
    RecoveryManager(store).process()
    assert len(store.list())==2


@pytest.mark.parametrize('kind',['length','empty','upstream_auth','timeout','connection','rate_limit','server'])
def test_only_proven_model_failure_can_schedule(tmp_path,kind):
    from app.recovery import RecoveryManager
    store,_,root=setup_task(tmp_path)
    error=model_failure(store,root,kind)
    RecoveryManager(store).schedule(root,error)
    assert store.get(root)['recovery_status']=='scheduled'


@pytest.mark.parametrize('error',['Invalid API key','HTTP 401','API timeout','limit reached','Script failed','SWARM_RECOVERABLE:00000000000000000000000000000000'])
def test_unproven_errors_and_old_tasks_never_auto_resume(tmp_path,error):
    from app.recovery import RecoveryManager
    store,_,root=setup_task(tmp_path)
    store.update(root,'failed',error=error)
    RecoveryManager(store).schedule(root,error)
    RecoveryManager(store).process()
    assert store.get(root)['resumed_as'] is None
    assert store.get(root)['recovery_status']=='blocked'
    with store.connect() as db: db.execute('UPDATE tasks SET auto_recover=0 WHERE id=?',(root,))
    error=model_failure(store,root)
    RecoveryManager(store).schedule(root,error)
    assert store.get(root)['resumed_as'] is None


def test_stop_cancels_pending_and_follows_already_created_continuation(tmp_path,monkeypatch):
    from app.recovery import RecoveryManager
    store,_,root=setup_task(tmp_path)
    monkeypatch.setattr('app.recovery.time.time',lambda:1000)
    RecoveryManager(store).schedule(root,model_failure(store,root))
    Engine(store).stop(root)
    monkeypatch.setattr('app.recovery.time.time',lambda:2000)
    RecoveryManager(store).process()
    assert store.get(root)['resumed_as'] is None
    from app.resume import continue_task
    child=continue_task(store,root)
    Engine(store).stop(root)
    assert store.get(child['id'])['stop_requested']==1
    assert store.get(child['id'])['state']=='stopped'


def test_parallel_recovery_and_manual_resume_share_one_continuation(tmp_path,monkeypatch):
    from app.recovery import RecoveryManager
    from app.resume import continue_task
    store,_,root=setup_task(tmp_path)
    monkeypatch.setattr('app.recovery.time.time',lambda:1000)
    RecoveryManager(store).schedule(root,model_failure(store,root))
    monkeypatch.setattr('app.recovery.time.time',lambda:2000)
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(lambda n: RecoveryManager(store).process() if n<2 else continue_task(store,root),range(3)))
    assert len(store.list())==2 and store.get(root)['resumed_as']


def test_three_attempt_cap_and_budget_exhaustion(tmp_path,monkeypatch):
    from app.recovery import RecoveryManager
    store,_,root=setup_task(tmp_path)
    clock=[1000];monkeypatch.setattr('app.recovery.time.time',lambda:clock[0])
    for attempt,delay in enumerate([15,60,180]):
        RecoveryManager(store).schedule(root,model_failure(store,root))
        assert store.get(root)['recovery_after']==clock[0]+delay
        clock[0]+=delay
        RecoveryManager(store).process()
        root=store.get(root)['resumed_as']
        assert store.get(root)['recovery_attempt']==attempt+1
    RecoveryManager(store).schedule(root,model_failure(store,root))
    clock[0]+=10000;RecoveryManager(store).process()
    assert store.get(root)['recovery_status']=='exhausted' and len(store.list())==4
    with store.connect() as db:
        db.execute('UPDATE tasks SET recovery_attempt=0,llm_calls=llm_limit WHERE id=?',(root,))
    RecoveryManager(store).schedule(root,store.get(root)['error'])
    assert store.get(root)['recovery_status']=='blocked'


def test_inflight_command_is_fenced_before_snapshot_then_recovery_retries(tmp_path,monkeypatch):
    from app.recovery import RecoveryManager
    store,queue,root=setup_task(tmp_path)
    job=queue.enqueue(root,root,'call:1','true',kind='command');queue.claim()
    clock=[1000];monkeypatch.setattr('app.recovery.time.time',lambda:clock[0])
    RecoveryManager(store).schedule(root,model_failure(store,root))
    clock[0]=1015;RecoveryManager(store).process()
    assert store.get(root)['stopped']==1 and store.get(root)['resumed_as'] is None
    queue.finish(job['id'],'stopped')
    clock[0]=1020;RecoveryManager(store).process()
    assert store.get(root)['resumed_as']


def test_engine_schedules_recovery_and_api_exposes_it(tmp_path,monkeypatch):
    from app.recovery import record_model_failure
    store,_,root=setup_task(tmp_path)
    marker=record_model_failure(store,root,root,'length','Truncated')
    state=SimpleNamespace(runtime_status=SimpleNamespace(name='FAILED'),serialized_custom_status='',to_json=lambda:{'failure_details':{'message':marker+': truncated'}})
    engine=Engine(store);engine.client=SimpleNamespace(get_workflow_state=lambda _:state)
    monkeypatch.setattr('app.recovery.time.time',lambda:1000)
    engine.reconcile()
    with TestClient(create_app(store,runtime_enabled=False),base_url='http://127.0.0.1:8788') as client:
        detail=client.get('/api/tasks/'+root).json()
        assert detail['recovery']['status']=='scheduled'
        assert client.get('/api/tasks').json()[0]['recovery']==detail['recovery']


def test_restart_between_failure_persistence_and_scheduling_recovers(tmp_path,monkeypatch):
    from app.recovery import RecoveryManager
    store,_,root=setup_task(tmp_path)
    model_failure(store,root)
    monkeypatch.setattr('app.recovery.time.time',lambda:1000)
    RecoveryManager(TaskStore(tmp_path)).process()
    assert store.get(root)['recovery_status']=='scheduled'
    monkeypatch.setattr('app.recovery.time.time',lambda:1015)
    RecoveryManager(TaskStore(tmp_path)).process()
    assert store.get(root)['resumed_as']


def test_stop_between_resume_transactions_cancels_inflight_manual_request(tmp_path,monkeypatch):
    import threading
    import uuid
    from app.resume import continue_task, ResumeConflict
    store,_,root=setup_task(tmp_path);store.update(root,'failed')
    entered,release=threading.Event(),threading.Event();original=uuid.uuid4
    def paused_uuid():
        entered.set();assert release.wait(5);return original()
    monkeypatch.setattr('app.resume.uuid.uuid4',paused_uuid)
    with ThreadPoolExecutor(1) as pool:
        pending=pool.submit(continue_task,store,root)
        assert entered.wait(5)
        Engine(store).stop(root);release.set()
        with pytest.raises(ResumeConflict):pending.result(timeout=5)
    assert store.get(root)['resumed_as'] is None
    # A new explicit request after Stop is still permitted.
    assert continue_task(store,root)['state']=='queued'


def test_conflict_handler_cannot_overwrite_a_concurrent_continuation(tmp_path,monkeypatch):
    from contextlib import contextmanager
    from app.recovery import RecoveryManager
    from app.resume import continue_task
    store,queue,root=setup_task(tmp_path)
    job=queue.enqueue(root,root,'call:1','true',kind='command');queue.claim()
    clock=[1000];monkeypatch.setattr('app.recovery.time.time',lambda:clock[0])
    RecoveryManager(store).schedule(root,model_failure(store,root));clock[0]=1015
    original=store.connect;inject=[True]
    class Connection:
        def __init__(self,db):self.db=db
        def __getattr__(self,key):return getattr(self.db,key)
        def execute(self,sql,*args):
            if sql.startswith('UPDATE tasks SET recovery_wait_since=') and inject[0]:
                inject[0]=False
                queue.finish(job['id'],'stopped')
                continue_task(store,root,automatic=True)
            return self.db.execute(sql,*args)
    @contextmanager
    def connection():
        with original() as db:yield Connection(db)
    monkeypatch.setattr(store,'connect',connection)
    RecoveryManager(store).process()
    task=store.get(root)
    assert task['resumed_as'] and task['recovery_status']=='continued'


def test_file_write_cannot_commit_after_resume_snapshot(tmp_path,monkeypatch):
    import threading
    from app.tools import make_tools,task_context,task_path
    from app.resume import continue_task
    store,_,root=setup_task(tmp_path)
    tool=next(t for t in make_tools(store) if t.name=='write_file')
    entered,release=threading.Event(),threading.Event()
    def paused_path(*args,**kwargs):
        path=task_path(*args,**kwargs);entered.set();assert release.wait(5);return path
    monkeypatch.setattr('app.tools.task_path',paused_path)
    def write():
        token=task_context.set((root,root))
        try:return tool(path='outputs/kept.txt',content='late write')
        finally:task_context.reset(token)
    with ThreadPoolExecutor(1) as pool:
        pending=pool.submit(write);assert entered.wait(5)
        store.update(root,'failed');child=continue_task(store,root);release.set()
        with pytest.raises(Exception):pending.result(timeout=5)
    assert (store.workspace(root)/'outputs/kept.txt').read_text()=='completed work'
    assert (store.workspace(child['id'])/'outputs/kept.txt').read_text()=='completed work'
