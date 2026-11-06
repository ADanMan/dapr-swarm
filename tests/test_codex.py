import json
import threading
import time

import pytest

from app.store import TaskStore


def test_codex_queue_is_idempotent_scoped_and_bounded(tmp_path):
    from app.codex_jobs import CodexJobs
    store = TaskStore(tmp_path)
    jobs = CodexJobs(store)
    task = store.create('Code', 1, 10, allow_codex=True)
    other = store.create('Other', 1, 10)
    store.update(task['id'], 'running')
    job = jobs.enqueue(task['id'], task['id'], 'call-1', 'Write code')
    assert jobs.enqueue(task['id'], task['id'], 'call-1', 'Write code')['id'] == job['id']
    with pytest.raises(ValueError):
        jobs.enqueue(other['id'], task['id'], 'call-2', 'Cross task')
    with pytest.raises(RuntimeError, match='лимит'):
        jobs.enqueue(task['id'], task['id'], 'call-2', 'Exceed budget')
    assert jobs.claim()['id'] == job['id']
    assert jobs.claim() is None
    store.stop(task['id'])
    with pytest.raises(RuntimeError):
        jobs.enqueue(task['id'], task['id'], 'call-3', 'Stopped')


@pytest.mark.parametrize('cancel', [False, True])
def test_codex_worker_pins_luna_and_stops_real_subprocess(tmp_path, monkeypatch, cancel):
    from app.codex_jobs import CodexJobs
    from app.codex_worker import command, run_job
    import sys
    store = TaskStore(tmp_path)
    jobs = CodexJobs(store)
    task = store.create('Code', 2, 10, allow_codex=True)
    store.update(task['id'], 'running')
    jobs.enqueue(task['id'], task['id'], 'call-1', 'Write code')
    job = jobs.claim()
    workspace = store.workspace(task['id'])
    args = command(workspace)
    assert args[args.index('--model') + 1] == 'gpt-6-luna'
    assert '--dangerously-bypass-approvals-and-sandbox' not in args
    assert 'permissions.swarm.network.enabled=false' in args
    profile = next(x for x in args if x.startswith('permissions.swarm.filesystem='))
    assert '":root"="deny"' in profile and '"outputs"="write"' in profile
    fixture = tmp_path / 'fake_codex.py'
    fixture.write_text('import json,time,sys\nsys.stdin.read()\n' +
        ('time.sleep(20)\n' if cancel else '') +
        'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"Done"}}))\n'
        'print(json.dumps({"type":"turn.completed"}))\n')
    monkeypatch.setattr('app.codex_worker.command', lambda _: [sys.executable, str(fixture)])
    stopper = threading.Timer(0.2, lambda: store.stop(task['id'])) if cancel else None
    if stopper:
        stopper.start()
    started = time.monotonic()
    run_job(jobs, job)
    result = jobs.get(job['id'])
    assert result['state'] == ('stopped' if cancel else 'completed')
    assert time.monotonic() - started < 5
    if not cancel:
        assert result['result'] == 'Done'


def test_worker_preparation_failure_is_contained(tmp_path):
    from app.codex_jobs import CodexJobs
    from app.codex_worker import run_job
    store = TaskStore(tmp_path)
    jobs = CodexJobs(store)
    task = store.create('Code', 2, 10, allow_codex=True)
    store.update(task['id'], 'running')
    jobs.enqueue(task['id'], task['id'], 'activity:1', 'Write code')
    job = jobs.claim()
    outputs = store.workspace(task['id']) / 'outputs'
    outputs.mkdir(exist_ok=True)
    (outputs / '.codex-tmp').write_text('ordinary file')
    run_job(jobs, job)
    assert jobs.get(job['id'])['state'] == 'failed'
    jobs.enqueue(task['id'], task['id'], 'activity:2', 'Next job')
    assert jobs.claim() is not None


def test_cleanup_terminates_children_after_parent_exit(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    from app.codex_worker import terminate
    pidfile = tmp_path / 'child.pid'
    parent = subprocess.Popen([sys.executable, '-c',
        'import subprocess,pathlib; p=subprocess.Popen(["/bin/sleep","30"]); pathlib.Path(' + repr(str(pidfile)) + ').write_text(str(p.pid))'],
        start_new_session=True)
    parent.wait(timeout=5)
    child = int(pidfile.read_text())
    try:
        terminate(parent)
        for _ in range(30):
            result = subprocess.run(['/bin/ps', '-p', str(child), '-o', 'stat='], capture_output=True, text=True)
            if result.returncode or result.stdout.strip().startswith('Z'):
                break
            time.sleep(0.05)
        else:
            pytest.fail('background child survives Codex exit')
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_glm_command_has_no_model_and_codex_requires_opt_in(tmp_path):
    from app.codex_jobs import CodexJobs
    from app.codex_worker import run_job
    store = TaskStore(tmp_path)
    jobs = CodexJobs(store)
    task = store.create('GLM code', 2, 10)
    store.update(task['id'], 'running')
    with pytest.raises(RuntimeError, match='выключен'):
        jobs.enqueue(task['id'], task['id'], 'activity:1', 'Use GPT')
    job = jobs.enqueue(task['id'], task['id'], 'activity:2',
                       'printf glm-only > outputs/result.txt; exit 7', kind='command')
    run_job(jobs, jobs.claim())
    result = jobs.get(job['id'])
    assert result['state'] == 'completed'
    evidence = json.loads(result['result'])
    assert evidence['exit_code'] == 7
    assert (store.workspace(task['id']) / 'outputs/result.txt').read_text() == 'glm-only'
    assert not list((store.directory / 'codex-jobs').glob('*.jsonl'))


def test_revoked_codex_opt_in_blocks_already_queued_job(tmp_path):
    from app.codex_jobs import CodexJobs
    store = TaskStore(tmp_path)
    jobs = CodexJobs(store)
    task = store.create('Code', 2, 10, allow_codex=True)
    store.update(task['id'], 'running')
    job = jobs.enqueue(task['id'], task['id'], 'activity:1', 'Do work')
    with store.connect() as db:
        db.execute('UPDATE tasks SET allow_codex=0 WHERE id=?', (task['id'],))
    assert jobs.claim() is None
    assert jobs.get(job['id'])['state'] == 'failed'


def test_restart_cleans_real_sandbox_command(tmp_path):
    import subprocess
    from app.codex_jobs import CodexJobs
    from app.codex_worker import shell_command, recover_interrupted
    store = TaskStore(tmp_path)
    jobs = CodexJobs(store)
    task = store.create('Code', 2, 10)
    store.update(task['id'], 'running')
    job = jobs.enqueue(task['id'], task['id'], 'activity:1', 'sleep 30', kind='command')
    jobs.claim()
    workspace = store.workspace(task['id'])
    (workspace / 'outputs').mkdir(exist_ok=True)
    child = subprocess.Popen(shell_command(workspace, '/bin/sleep 30'), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with store.connect() as db:
            db.execute('UPDATE codex_jobs SET pid=? WHERE id=?', (child.pid, job['id']))
        time.sleep(0.1)
        recover_interrupted(jobs)
        child.wait(timeout=3)
        assert jobs.get(job['id'])['state'] == 'failed'
    finally:
        if child.poll() is None:
            import os, signal
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)


def test_fast_command_output_limit_is_enforced_after_exit(tmp_path):
    from app.codex_jobs import CodexJobs
    from app.codex_worker import run_job
    store = TaskStore(tmp_path)
    jobs = CodexJobs(store)
    task = store.create('Code', 2, 10)
    store.update(task['id'], 'running')
    job = jobs.enqueue(task['id'], task['id'], 'activity:1',
        "python3 -c 'import sys; sys.stdout.write(\"x\" * (5 * 1024 * 1024))'", kind='command')
    run_job(jobs, jobs.claim())
    assert jobs.get(job['id'])['state'] == 'failed'
    assert 'журнала' in jobs.get(job['id'])['error']


@pytest.mark.parametrize('failure', [False, True])
def test_terminal_job_state_is_published_only_after_process_cleanup(tmp_path, monkeypatch, failure):
    import subprocess
    from app.codex_jobs import CodexJobs
    from app.codex_worker import run_job
    store = TaskStore(tmp_path)
    jobs = CodexJobs(store)
    task = store.create('Cleanup before continuation', 2, 10)
    store.update(task['id'], 'running')
    job = jobs.enqueue(task['id'], task['id'], 'activity:1', 'synthetic', kind='command')
    claimed = jobs.claim()
    if failure:
        monkeypatch.setattr('app.codex_worker.shell_command', lambda *_: ['/bin/sleep', '30'])
        original_pid = jobs.set_pid
        def error_after_start(job_id, pid):
            original_pid(job_id, pid)
            raise RuntimeError('Synthetic worker failure while process is alive')
        monkeypatch.setattr(jobs, 'set_pid', error_after_start)
    else:
        monkeypatch.setattr('app.codex_worker.shell_command', lambda *_:
                            ['/bin/sh', '-c', '/bin/sleep 30 & echo $! > outputs/child.pid'])
    original_finish = jobs.finish
    observed = []
    def finish_after_cleanup(job_id, state, result='', error=''):
        pid = jobs.get(job_id)['pid'] if failure else int((store.workspace(task['id'])/'outputs/child.pid').read_text())
        status = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
        observed.append(status.returncode != 0 or status.stdout.strip().startswith('Z'))
        return original_finish(job_id, state, result, error)
    monkeypatch.setattr(jobs, 'finish', finish_after_cleanup)
    run_job(jobs, claimed)
    assert observed == [True], 'Task became resumable while its old process was still alive'
    assert jobs.get(job['id'])['state'] == ('failed' if failure else 'completed')
