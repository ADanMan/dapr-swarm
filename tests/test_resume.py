import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.codex_jobs import CodexJobs
from app.main import Engine, create_app
from app.store import TaskStore


@pytest.fixture
def context(tmp_path):
    store = TaskStore(tmp_path / 'data')
    with TestClient(create_app(store, runtime_enabled=False), base_url='http://127.0.0.1:8788') as client:
        yield store, client


@pytest.mark.parametrize('state', ['failed', 'stopped'])
def test_resume_preserves_context_files_policy_and_is_idempotent(context, state):
    store, client = context
    task = store.create('Continue analysis', 4, 30)
    root = store.workspace(task['id'])
    (root / 'inputs').mkdir()
    (root / 'inputs/input.txt').write_text('original input')
    (root / 'outputs').mkdir()
    (root / 'outputs/progress.txt').write_text('phase one complete')
    store.event(task['id'], task['id'], 'message', 'Phase one completed; phase two remains')
    store.reserve_call(task['id'])
    store.update(task['id'], 'failed', error='Provider timeout')
    if state == 'stopped':
        store.stop(task['id'])
    route = f"/api/tasks/{task['id']}/resume"
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: client.post(route), range(2)))
    assert all(r.status_code == 200 for r in responses)
    resumed = responses[0].json()
    assert resumed['id'] != task['id']
    assert responses[1].json()['id'] == resumed['id']
    assert resumed['resumed_from'] == task['id'] and resumed['state'] == 'queued'
    assert resumed['allow_codex'] == 0 and resumed['llm_calls'] == 0
    assert resumed['agent_limit'] == 4 and resumed['llm_limit'] == 30
    old = store.get(task['id'])
    assert old['resumed_as'] == resumed['id'] and old['stopped'] == 1 and old['llm_calls'] == 1
    assert old['state'] == state and old['error'] == 'Provider timeout'
    copied = store.workspace(resumed['id'])
    assert (copied/'inputs/input.txt').read_text() == 'original input'
    assert (copied/'outputs/progress.txt').read_text() == 'phase one complete'
    note = json.loads((copied/f"notes/resume-{task['id']}.json").read_text())
    assert note['source_id'] == task['id'] and 'Provider timeout' in note['error']
    assert 'Phase one' in note['events'][0]['content']
    (copied/'outputs/progress.txt').write_text('new work')
    assert (root/'outputs/progress.txt').read_text() == 'phase one complete'
    with pytest.raises(RuntimeError):
        store.reserve_call(task['id'])
    engine = Engine(store)
    scheduled = []
    engine.lead = SimpleNamespace(agent_workflow_name='coordinator')
    engine.client = SimpleNamespace(get_workflow_state=lambda _: None,
        schedule_new_workflow=lambda *args, **kwargs: scheduled.append(kwargs))
    engine.reconcile()
    assert len(scheduled) == 1 and scheduled[0]['instance_id'] == resumed['id']
    assert f"notes/resume-{task['id']}.json" in scheduled[0]['input']['task']


def test_resume_rejects_active_completed_missing_and_cross_origin(context):
    store, client = context
    for state in ['preparing', 'queued', 'running', 'completed']:
        task = store.create('Not resumable', 2, 10)
        store.update(task['id'], state)
        assert client.post(f"/api/tasks/{task['id']}/resume").status_code == 409
        assert store.get(task['id'])['stopped'] == 0
    assert client.post('/api/tasks/'+'0'*32+'/resume').status_code == 404
    assert client.post('/api/tasks/'+'0'*32+'/resume', headers={'Origin':'https://evil.example'}).status_code == 403


def test_resume_fences_old_worker_before_copy(context):
    store, client = context
    task = store.create('Running command', 2, 10)
    store.update(task['id'], 'running')
    queue = CodexJobs(store)
    queued = queue.enqueue(task['id'], task['id'], 'call:1', 'printf hi', kind='command')
    queue.claim()
    store.update(task['id'], 'failed', error='Parent failed')
    route = f"/api/tasks/{task['id']}/resume"
    assert client.post(route).status_code == 409
    assert store.get(task['id'])['stopped'] == 1
    assert len(store.list()) == 1
    queue.finish(queued['id'], 'stopped')
    assert client.post(route).status_code == 200


def test_resume_symlink_and_copy_failure_leave_source_retryable(context, tmp_path, monkeypatch):
    store, client = context
    task = store.create('Unsafe artifact', 2, 10)
    store.update(task['id'], 'failed')
    root = store.workspace(task['id'])
    (root/'outputs').mkdir()
    (root/'outputs/link').symlink_to(tmp_path/'outside')
    route = f"/api/tasks/{task['id']}/resume"
    assert client.post(route).status_code == 422
    assert len(store.list()) == 1 and store.get(task['id'])['resumed_as'] is None
    (root/'outputs/link').unlink()
    assert client.post(route).status_code == 200


def test_resume_preserves_safe_executable_permissions(context):
    import stat
    store, client = context
    source = store.create('Keep runnable scripts', 2, 10)
    script = store.workspace(source['id'])/'outputs/run.sh'
    script.parent.mkdir()
    script.write_text('#!/bin/sh\nprintf resumed\n')
    script.chmod(0o755)
    store.stop(source['id'])
    resumed = client.post(f"/api/tasks/{source['id']}/resume").json()
    copied = store.workspace(resumed['id'])/'outputs/run.sh'
    assert copied.read_bytes() == script.read_bytes()
    assert stat.S_IMODE(copied.stat().st_mode) == 0o755


def test_failed_snapshot_never_queues_partial_continuation(context, monkeypatch):
    from pathlib import Path
    store, client = context
    source = store.create('Snapshot failure', 2, 10)
    artifact = store.workspace(source['id'])/'outputs/result.txt'
    artifact.parent.mkdir()
    artifact.write_text('preserve source')
    store.stop(source['id'])
    original_open = Path.open
    def fail_destination(self, mode='r', *args, **kwargs):
        if mode == 'xb':
            raise OSError('synthetic disk full')
        return original_open(self, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', fail_destination)
    route = f"/api/tasks/{source['id']}/resume"
    assert client.post(route).status_code == 507
    assert len(store.list()) == 1
    assert store.get(source['id'])['resumed_as'] is None
    assert artifact.read_text() == 'preserve source'
    assert len(list((store.directory/'workspaces').iterdir())) == 1
    monkeypatch.setattr(Path, 'open', original_open)
    assert client.post(route).status_code == 200


def test_legacy_gpt_task_continues_with_glm_only(context):
    store,client=context
    old=store.create('Legacy optional GPT',2,10,allow_codex=True)
    store.stop(old['id'])
    resumed=client.post(f"/api/tasks/{old['id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()['allow_codex'] == 0
    assert store.get(old['id'])['allow_codex'] == 1
