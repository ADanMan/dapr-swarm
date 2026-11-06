"""Host worker must never open the container-owned SQLite database."""
import threading
import socket
import time

import uvicorn


def test_host_worker_executes_through_http_without_own_database(tmp_path):
    from app.main import create_app
    from app.store import TaskStore
    from app.codex_jobs import CodexJobs
    from app.codex_worker import run_job
    from app.worker_client import RemoteStore, RemoteJobs
    server_store = TaskStore(tmp_path/'server')
    task = server_store.create('Command', 2, 10)
    server_store.update(task['id'], 'running')
    queue = CodexJobs(server_store)
    queued = queue.enqueue(task['id'], task['id'], 'activity:1', 'printf done > outputs/proof.txt', kind='command')
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    server=uvicorn.Server(uvicorn.Config(create_app(server_store,runtime_enabled=False),log_level='critical',access_log=False))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    until=time.monotonic()+5
    while not server.started:
        assert time.monotonic()<until
        time.sleep(.01)
    try:
        remote=RemoteStore(tmp_path/'host',f'http://127.0.0.1:{sock.getsockname()[1]}')
        remote.workspace(task['id']).mkdir(parents=True)
        jobs=RemoteJobs(remote)
        claimed=jobs.claim()
        assert claimed['id']==queued['id']
        run_job(jobs,claimed)
        assert queue.get(queued['id'])['state']=='completed'
        assert (remote.workspace(task['id'])/'outputs/proof.txt').read_text()=='done'
        assert not (remote.directory/'tasks.sqlite').exists()
        assert server_store.events(task['id'])[-1]['kind']=='command'
    finally:
        server.should_exit=True;thread.join(5);sock.close()


def test_lost_finish_ack_keeps_completed_job_and_worker_recoverable(tmp_path, monkeypatch):
    import pytest
    from app.main import create_app
    from app.store import TaskStore
    from app.codex_jobs import CodexJobs
    from app.codex_worker import run_job, recover_interrupted
    from app.worker_client import RemoteStore, RemoteJobs, WorkerUnavailable
    server_store = TaskStore(tmp_path / 'server')
    task = server_store.create('Lost HTTP acknowledgement', 2, 10)
    server_store.update(task['id'], 'running')
    queue = CodexJobs(server_store)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(create_app(server_store, runtime_enabled=False),
                           log_level='critical', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    until = time.monotonic() + 5
    while not server.started:
        assert time.monotonic() < until
        time.sleep(0.01)
    try:
        remote = RemoteStore(tmp_path / 'host', f'http://127.0.0.1:{sock.getsockname()[1]}')
        remote.workspace(task['id']).mkdir(parents=True)
        jobs = RemoteJobs(remote)
        original = remote.request
        for index, lost_route in enumerate(('finish', 'events')):
            def lost_response(route, data=None):
                result = original(route, data)
                if ((lost_route == 'finish' and route.endswith('/finish') and data['state'] == 'completed')
                    or (lost_route == 'events' and route.endswith('/events') and data['content'].startswith('{'))):
                    raise WorkerUnavailable('Simulated lost response after server commit')
                return result
            monkeypatch.setattr(remote, 'request', lost_response)
            queued = queue.enqueue(task['id'], task['id'], f'activity:{index}', 'printf done', kind='command')
            with pytest.raises(WorkerUnavailable):
                run_job(jobs, jobs.claim())
            assert queue.get(queued['id'])['state'] == 'completed'
            monkeypatch.setattr(remote, 'request', original)
            # Same reconciliation that main runs after WorkerUnavailable; no replay.
            recover_interrupted(jobs)
            assert queue.get(queued['id'])['state'] == 'completed'
        queued = queue.enqueue(task['id'], task['id'], 'activity:next', 'printf next', kind='command')
        run_job(jobs, jobs.claim())
        assert queue.get(queued['id'])['state'] == 'completed'
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
