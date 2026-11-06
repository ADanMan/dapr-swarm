import io
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    from app.main import create_app
    from app.store import TaskStore
    with TestClient(create_app(TaskStore(tmp_path), runtime_enabled=False), base_url='http://127.0.0.1:8788') as client:
        yield client


def test_tasks_require_valid_input_and_same_origin(client):
    assert client.post('/api/tasks', data={'prompt': ' '}).status_code == 422
    assert client.post('/api/tasks', data={'prompt': 'Hello', 'agent_limit': '2001'}).status_code == 422
    assert client.post('/api/tasks', data={'prompt': 'Hello'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/tasks/unknown').status_code == 404


def test_upload_download_and_stop(client):
    created = client.post('/api/tasks', data={'prompt': 'Прочитай файл'}, files={'files': ('input.txt', b'hello')})
    assert created.status_code == 201
    task_id = created.json()['id']
    detail = client.get('/api/tasks/' + task_id).json()
    assert detail['files'][0]['path'] == 'inputs/input.txt'
    response = client.get(f'/api/tasks/{task_id}/files/inputs/input.txt')
    assert response.content == b'hello'
    assert 'attachment' in response.headers['content-disposition']
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert client.post(f'/api/tasks/{task_id}/stop').status_code == 200
    assert client.get('/api/tasks/' + task_id).json()['state'] == 'stopped'


def test_oversized_file_and_traversal_rejected(client):
    response = client.post('/api/tasks', data={'prompt': 'Read'}, files={'files': ('../escape.txt', b'hello')})
    assert response.status_code == 422
    response = client.post('/api/tasks', data={'prompt': 'Read'}, files={'files': ('large.txt', io.BytesIO(b'x' * (10 * 1024 * 1024 + 1)))})
    assert response.status_code == 413
    assert client.get('/api/tasks').json() == []


def test_chunked_body_is_bounded_before_form_parsing(client, monkeypatch):
    from app.main import BodyLimitMiddleware
    monkeypatch.setattr(BodyLimitMiddleware, 'limit', 1024)
    body = b'--bound\r\nContent-Disposition: form-data; name="files"; filename="x.txt"\r\n\r\n' + b'x' * 2000 + b'\r\n--bound--\r\n'
    response = client.post('/api/tasks', content=iter([body]), headers={'Content-Type': 'multipart/form-data; boundary=bound'})
    assert response.status_code == 413
    assert client.get('/api/tasks').json() == []


def test_upload_write_failure_never_queues_task(client, monkeypatch):
    from pathlib import Path
    def fail_write(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(Path, 'write_bytes', fail_write)
    response = client.post('/api/tasks', data={'prompt': 'Read'}, files={'files': ('x.txt', b'hello')})
    assert response.status_code == 507
    tasks = client.get('/api/tasks').json()
    assert len(tasks) == 1
    assert tasks[0]['state'] == 'failed'


def test_iteration_exhaustion_is_not_success(tmp_path):
    from app.main import Engine
    from app.store import TaskStore
    from types import SimpleNamespace
    store = TaskStore(tmp_path)
    task = store.create('Unfinished', 3, 10)
    store.update(task['id'], 'running')
    engine = Engine(store)
    state = SimpleNamespace(runtime_status=SimpleNamespace(name='COMPLETED'),
        serialized_custom_status='"max_iterations_reached"', serialized_output='{"content":"Starting work"}')
    engine.client = SimpleNamespace(get_workflow_state=lambda task_id: state)
    engine.reconcile()
    assert store.get(task['id'])['state'] == 'failed'
    assert store.flows(task['id'])[0]['state'] == 'failed'
    assert not (store.workspace(task['id']) / 'outputs/final-answer.md').exists()


@pytest.mark.parametrize('status,output', [('COMPLETED', '{"content":null}'), ('FAILED', None)])
def test_empty_or_failed_workflow_reconciles_to_failure(tmp_path, status, output):
    from app.main import Engine
    from app.store import TaskStore
    from types import SimpleNamespace
    store = TaskStore(tmp_path)
    task = store.create('CLI', 2, 10)
    store.update(task['id'], 'running')
    state = SimpleNamespace(runtime_status=SimpleNamespace(name=status), serialized_custom_status=None,
                            serialized_output=output, to_json=lambda:{'failure_details':{'message':'Provider failed'}})
    engine = Engine(store)
    engine.client = SimpleNamespace(get_workflow_state=lambda _:state)
    engine.reconcile()
    result = store.get(task['id'])
    assert result['state'] == 'failed' and result['error']
    assert not (store.workspace(task['id'])/'outputs/final-answer.md').exists()


def test_importing_api_does_not_open_production_database(tmp_path):
    import os
    import subprocess
    import sys
    data = tmp_path / 'must-not-exist'
    result = subprocess.run([sys.executable, '-c', 'import app.main'],
        env={**os.environ, 'SWARM_DATA_DIR': str(data)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not data.exists(), 'Importing API must not create or open a live database'


def test_new_task_budget_defaults_and_upper_boundary(client):
    task = client.post('/api/tasks', data={'prompt':'Default budget'}).json()
    assert (task['agent_limit'], task['llm_limit']) == (2000, 20000)
    assert client.post('/api/tasks', data={'prompt':'Too many agents','agent_limit':2001}).status_code == 422
    assert client.post('/api/tasks', data={'prompt':'Too many calls','llm_limit':20001}).status_code == 422


def test_hierarchical_tasks_reject_gpt_opt_in(client):
    response=client.post('/api/tasks', data={'prompt':'All GLM','allow_codex':'true'})
    assert response.status_code == 422
    assert client.get('/api/tasks').json() == []
