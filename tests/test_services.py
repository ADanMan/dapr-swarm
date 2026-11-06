from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest


def services():
    from app.store import TaskStore
    from app.tools import task_path, validate_public_url
    return TaskStore, task_path, validate_public_url


def test_task_persistence_and_child_isolation(tmp_path):
    TaskStore, _, _ = services()
    store = TaskStore(tmp_path)
    a = store.create('Первая задача', 3, 10)
    b = store.create('Вторая задача', 3, 10)
    store.register_flow('child-a', a['id'], 'researcher')
    store.register_flow('child-b', b['id'], 'analyst')
    reopened = TaskStore(tmp_path)
    assert reopened.root_for('child-a') == a['id']
    assert reopened.root_for('child-b') == b['id']
    assert reopened.get(a['id'])['prompt'] == 'Первая задача'


def test_call_budget_is_atomic_and_stop_blocks_children(tmp_path):
    TaskStore, _, _ = services()
    store = TaskStore(tmp_path)
    task = store.create('Budget', 1, 3)
    def reserve(_):
        try:
            store.reserve_call(task['id'])
            return True
        except RuntimeError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(20))) == 3
    store.register_flow('child', task['id'], 'researcher')
    store.register_flow('child', task['id'], 'researcher')
    with pytest.raises(RuntimeError):
        store.register_flow('extra', task['id'], 'researcher')
    store.stop(task['id'])
    with pytest.raises(RuntimeError):
        store.ensure_active(task['id'])
    with pytest.raises(RuntimeError):
        store.register_flow('late', task['id'], 'analyst')


def test_workspace_rejects_escape_and_symlinks(tmp_path):
    _, task_path, _ = services()
    root = tmp_path / 'task'
    root.mkdir()
    (root / 'link').symlink_to(tmp_path, target_is_directory=True)
    for name in ['../secret', '/etc/passwd', 'link/secret', 'a/../../secret']:
        with pytest.raises(ValueError):
            task_path(root, name)
    assert task_path(root, 'notes/report.md') == root / 'notes/report.md'


def test_url_policy_blocks_internal_addresses():
    _, _, validate = services()
    for url in ['file:///etc/passwd', 'http://127.0.0.1', 'http://[::1]',
                'http://169.254.169.254/', 'http://10.0.0.1',
                'https://user:password@example.org', 'https://example.org:8080']:
        with pytest.raises(ValueError):
            validate(url)


def test_preparing_task_cannot_be_scheduled(tmp_path):
    TaskStore, _, _ = services()
    store = TaskStore(tmp_path)
    task = store.create('Read uploaded input', 2, 10)
    assert store.active() == []
    store.update(task['id'], 'queued')
    assert [t['id'] for t in store.active()] == [task['id']]


def test_fetch_has_process_deadline(tmp_path, monkeypatch):
    import app.tools as tools
    import time
    slow = tmp_path / 'slow-worker'
    slow.write_text('#!/bin/sh\nexec /bin/sleep 5\n')
    slow.chmod(0o700)
    monkeypatch.setattr(tools.sys, 'executable', str(slow))
    monkeypatch.setattr(tools, 'FETCH_TIMEOUT_SECONDS', 0.1)
    started = time.monotonic()
    with pytest.raises(ValueError, match='время'):
        tools.fetch_public_page('https://example.org')
    assert time.monotonic() - started < 2
