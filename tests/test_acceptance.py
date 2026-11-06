"""Acceptance is evidence from a scoped command, never model prose."""
import hashlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.codex_jobs import CodexJobs
from app.store import TaskStore


def setup_task(tmp_path):
    store = TaskStore(tmp_path)
    root = store.create('Verify artifacts', 10, 100)['id']
    store.update(root, 'running')
    store.register_flow('reviewer', root, 'swarm_reviewer')
    workspace = store.workspace(root)
    (workspace / 'outputs').mkdir()
    (workspace / 'outputs/result.txt').write_text('answer')
    return store, root, workspace, CodexJobs(store)


def registry(store):
    assert importlib.util.find_spec('app.acceptance') is not None, 'Structured acceptance registry is missing'
    from app.acceptance import AcceptanceStore
    return AcceptanceStore(store)


def enqueue(jobs, root, call='check', paths=None, criteria=None, flow='reviewer'):
    return jobs.enqueue(root, flow, call, 'test -s outputs/result.txt', kind='command', validation={
        'artifact_paths': paths or ['outputs'], 'criteria': criteria or ['Result is nonempty']})


def finish(jobs, job, code=0):
    jobs.finish(job['id'], 'completed', json.dumps({'exit_code': code, 'stdout': 'PASSED', 'stderr': ''}))


def test_no_model_text_or_ordinary_job_can_establish_acceptance(tmp_path):
    store, root, _, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    job = jobs.enqueue(root, 'reviewer', 'ordinary', 'true', kind='command')
    finish(jobs, job)
    store.update(root, 'completed', result='ACCEPTED: all checks pass')
    assert acceptance.summary(root)['status'] == 'unverified'


def test_durable_enqueue_and_finish_before_root_completion(tmp_path):
    store, root, _, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    job = enqueue(jobs, root)
    assert acceptance.summary(root)['status'] == 'checking'
    reopened = TaskStore(tmp_path)
    worker = CodexJobs(reopened)
    assert worker.claim()['id'] == job['id']
    assert registry(reopened).summary(root)['job_id'] == job['id']
    finish(worker, job)
    assert registry(reopened).summary(root)['status'] == 'checking'
    store.update(root, 'completed')
    result = registry(TaskStore(tmp_path)).summary(root)
    assert result['status'] == 'passed'
    assert result['criteria'] == ['Result is nonempty']
    assert result['command'] == 'test -s outputs/result.txt'
    assert result['flow'] == 'reviewer'
    assert result['checked_at'] and result['exit_code'] == 0
    assert result['artifacts'] == [{'path': 'outputs/result.txt', 'sha256': hashlib.sha256(b'answer').hexdigest()}]


@pytest.mark.parametrize('code', [1, 7, None, False, '0'])
def test_nonzero_or_unstructured_exit_never_passes(tmp_path, code):
    store, root, _, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    job = enqueue(jobs, root)
    finish(jobs, job, code)
    store.update(root, 'completed')
    assert acceptance.summary(root)['status'] == 'failed'


@pytest.mark.parametrize('mutation', ['content', 'new_file', 'empty_dir', 'delete', 'symlink'])
def test_snapshot_changes_after_finish_make_receipt_stale(tmp_path, mutation):
    store, root, workspace, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    job = enqueue(jobs, root)
    finish(jobs, job)
    store.update(root, 'completed')
    assert acceptance.summary(root)['status'] == 'passed'
    target = workspace / 'outputs/result.txt'
    if mutation == 'content': target.write_text('changed')
    elif mutation == 'new_file': (workspace / 'outputs/new.txt').write_text('extra')
    elif mutation == 'empty_dir': (workspace / 'outputs/empty').mkdir()
    elif mutation == 'delete': target.unlink()
    else:
        target.unlink()
        target.symlink_to(tmp_path / 'outside')
    assert acceptance.summary(root)['status'] == 'stale'
    assert registry(TaskStore(tmp_path)).summary(root)['status'] == 'stale'


def test_changes_during_command_are_not_accepted_even_if_later_restored(tmp_path):
    store, root, workspace, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    job = enqueue(jobs, root)
    (workspace / 'outputs/result.txt').write_text('changed')
    finish(jobs, job)
    (workspace / 'outputs/result.txt').write_text('answer')
    store.update(root, 'completed')
    assert acceptance.summary(root)['status'] == 'stale'


@pytest.mark.parametrize('path', ['../outside', '/tmp/outside', 'outputs/missing', 'outputs/link'])
def test_invalid_paths_never_leave_claimable_job(tmp_path, path):
    store, root, workspace, jobs = setup_task(tmp_path)
    registry(store)
    (workspace / 'outputs/link').symlink_to(tmp_path)
    with pytest.raises(ValueError):
        enqueue(jobs, root, paths=[path])
    assert jobs.claim() is None
    assert jobs.list(root) == []


def test_symlinks_nested_in_directory_are_rejected(tmp_path):
    store, root, workspace, jobs = setup_task(tmp_path)
    registry(store)
    (workspace / 'outputs/link').symlink_to(tmp_path)
    with pytest.raises(ValueError): enqueue(jobs, root)
    assert jobs.claim() is None


def test_validation_requires_reviewer_same_root_and_command_kind(tmp_path):
    store, root, _, jobs = setup_task(tmp_path)
    registry(store)
    other = store.create('Other', 10, 100)['id']
    store.update(other, 'running')
    with pytest.raises(ValueError): enqueue(jobs, root, flow=root)
    with pytest.raises(ValueError): enqueue(jobs, other)
    with pytest.raises(ValueError):
        jobs.enqueue(root, 'reviewer', 'model', 'true', kind='codex', validation={
            'artifact_paths': ['outputs'], 'criteria': ['check']})
    assert jobs.claim() is None


def test_replay_preserves_snapshot_and_cannot_relabel_an_ordinary_job(tmp_path):
    store, root, workspace, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: enqueue(jobs, root)['id'], range(8)))
    assert len(set(ids)) == 1
    (workspace / 'outputs/result.txt').write_text('changed')
    assert enqueue(jobs, root)['id'] == ids[0]
    assert len(jobs.list(root)) == 1
    with pytest.raises(ValueError): enqueue(jobs, root, criteria=['Different criterion'])
    jobs.enqueue(root, 'reviewer', 'ordinary', 'test -s outputs/result.txt', kind='command')
    with pytest.raises(ValueError): enqueue(jobs, root, call='ordinary')
    finish(jobs, {'id': ids[0]})
    store.update(root, 'completed')
    assert acceptance.summary(root)['status'] == 'stale'


def test_explicit_admin_receipt_checks_task_job_role_and_hashes(tmp_path):
    store, root, _, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    job = jobs.enqueue(root, 'reviewer', 'independent', 'true', kind='command')
    finish(jobs, job)
    hashes = {'outputs/result.txt': hashlib.sha256(b'answer').hexdigest()}
    with pytest.raises(ValueError): acceptance.import_verified_receipt(root, job['id'], hashes, ['Audited'])
    store.update(root, 'completed')
    with pytest.raises(ValueError): acceptance.import_verified_receipt(root, job['id'], {'outputs/result.txt': '0' * 64}, ['Audited'])
    acceptance.import_verified_receipt(root, job['id'], hashes, ['Audited'])
    assert acceptance.summary(root)['status'] == 'passed'
    assert acceptance.summary(root)['source'] == 'external_audit'
    assert acceptance.import_verified_receipt(root, job['id'], hashes, ['Audited'])['status'] == 'passed'


@pytest.mark.parametrize('invalid', ['role', 'foreign', 'failed'])
def test_admin_receipt_rejects_untrusted_job(tmp_path, invalid):
    store, root, _, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    flow = root if invalid == 'role' else 'reviewer'
    job = jobs.enqueue(root, flow, 'old', 'true', kind='command')
    finish(jobs, job, 1 if invalid == 'failed' else 0)
    store.update(root, 'completed')
    receipt_root = root
    if invalid == 'foreign':
        receipt_root = store.create('Other', 10, 100)['id']
        store.update(receipt_root, 'completed')
    with pytest.raises(ValueError):
        acceptance.import_verified_receipt(receipt_root, job['id'], {
            'outputs/result.txt': hashlib.sha256(b'answer').hexdigest()}, ['Audited'])


def test_run_validation_tool_rejects_nonreviewer_before_worker_access(tmp_path):
    from app.tools import make_tools, task_context
    from dapr_agents.types.exceptions import ToolError
    store, root, _, jobs = setup_task(tmp_path)
    tools = {tool.name: tool for tool in make_tools(store)}
    assert 'run_validation' in tools
    token = task_context.set((root, root))
    try:
        with pytest.raises(ToolError, match='swarm_reviewer'):
            tools['run_validation'](command='true', artifact_paths=['outputs'], criteria=['Nonempty'])
        assert jobs.claim() is None
    finally:
        task_context.reset(token)


def test_real_validation_tool_uses_worker_and_accepts_first_directory_check(tmp_path):
    import threading
    import time
    from app.codex_worker import heartbeat, run_job
    from app.tools import make_tools, task_context, tool_call_context
    store, root, _, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    heartbeat(store)
    done = threading.Event()
    def worker():
        deadline = time.monotonic() + 10
        while not done.is_set() and time.monotonic() < deadline:
            job = jobs.claim()
            if job:
                run_job(jobs, job)
                return
            time.sleep(0.02)
    thread = threading.Thread(target=worker)
    thread.start()
    context_token = task_context.set((root, 'reviewer'))
    call_token = tool_call_context.set('real-validation')
    try:
        tool = next(tool for tool in make_tools(store) if tool.name == 'run_validation')
        result = tool(command='test -s outputs/result.txt', artifact_paths=['outputs'], criteria=['Result is nonempty'])
        assert json.loads(result)['exit_code'] == 0
    finally:
        done.set()
        thread.join(timeout=12)
        task_context.reset(context_token)
        tool_call_context.reset(call_token)
    store.update(root, 'completed')
    assert acceptance.summary(root)['status'] == 'passed'


def test_bounded_snapshot_failure_rolls_back_command(tmp_path, monkeypatch):
    store, root, workspace, jobs = setup_task(tmp_path)
    registry(store)
    monkeypatch.setattr('app.acceptance.MAX_ARTIFACTS', 1)
    (workspace / 'outputs/second.txt').write_text('second')
    with pytest.raises(ValueError): enqueue(jobs, root)
    assert jobs.claim() is None


def test_worker_scratch_is_not_an_artifact_but_other_hidden_files_are(tmp_path):
    store, root, workspace, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    scratch = workspace / 'outputs/.codex-tmp'
    scratch.mkdir()
    (scratch / 'temporary').write_text('runtime')
    with pytest.raises(ValueError): enqueue(jobs, root, paths=['outputs/.codex-tmp/temporary'])
    job = enqueue(jobs, root)
    (scratch / 'temporary').write_text('changed runtime')
    finish(jobs, job)
    store.update(root, 'completed')
    assert acceptance.summary(root)['status'] == 'passed'
    (workspace / 'outputs/.report').write_text('new artifact')
    assert acceptance.summary(root)['status'] == 'stale'


def test_finish_receipt_and_job_completion_commit_together(tmp_path):
    import sqlite3
    store, root, _, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    job = enqueue(jobs, root)
    jobs.claim()
    with store.connect() as db:
        db.execute("""CREATE TRIGGER fail_receipt BEFORE UPDATE ON acceptance_checks
                      BEGIN SELECT RAISE(ABORT, 'simulated interrupted commit'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='interrupted commit'):
        finish(jobs, job)
    assert jobs.get(job['id'])['state'] == 'running'
    assert acceptance.summary(root)['checked_at'] is None
    with store.connect() as db:
        db.execute('DROP TRIGGER fail_receipt')
    finish(CodexJobs(TaskStore(tmp_path)), job)
    store.update(root, 'completed')
    assert registry(TaskStore(tmp_path)).summary(root)['status'] == 'passed'


def test_fifo_is_rejected_without_blocking_or_leaving_a_job(tmp_path):
    import os
    store, root, workspace, jobs = setup_task(tmp_path)
    registry(store)
    os.mkfifo(workspace / 'outputs/pipe')
    with pytest.raises(ValueError): enqueue(jobs, root)
    assert jobs.claim() is None


@pytest.mark.parametrize('metadata', [
    {'artifact_paths': [], 'criteria': ['check']},
    {'artifact_paths': ['outputs'], 'criteria': []},
    {'artifact_paths': ['outputs'], 'criteria': [' ']},
    {'artifact_paths': ['outputs'], 'criteria': ['check'], 'passed': True},
])
def test_malformed_or_model_supplied_pass_metadata_is_rejected(tmp_path, metadata):
    store, root, _, jobs = setup_task(tmp_path)
    registry(store)
    with pytest.raises(ValueError):
        jobs.enqueue(root, 'reviewer', 'invalid', 'true', kind='command', validation=metadata)
    assert jobs.claim() is None


def test_runtime_final_answer_does_not_invalidate_directory_acceptance(tmp_path):
    store, root, workspace, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    job = enqueue(jobs, root, paths=['outputs'])
    finish(jobs, job)
    (workspace / 'outputs/final-answer.md').write_text('Runtime final answer')
    store.update(root, 'completed')
    assert acceptance.summary(root)['status'] == 'passed'
    (workspace / 'outputs/new-artifact.md').write_text('New artifact')
    assert acceptance.summary(root)['status'] == 'stale'


def test_explicit_final_answer_remains_hash_checked_for_admin_receipt(tmp_path):
    store, root, workspace, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    job = jobs.enqueue(root, 'reviewer', 'independent', 'true', kind='command')
    finish(jobs, job)
    (workspace / 'outputs/final-answer.md').write_text('Runtime final answer')
    store.update(root, 'completed')
    acceptance.import_verified_receipt(root, job['id'], {
        'outputs/final-answer.md': hashlib.sha256(b'Runtime final answer').hexdigest()}, ['Final answer reviewed'])
    assert acceptance.summary(root)['status'] == 'passed'
    (workspace / 'outputs/final-answer.md').write_text('Changed final answer')
    assert acceptance.summary(root)['status'] == 'stale'


def test_final_answer_exclusion_does_not_hide_a_directory_of_artifacts(tmp_path):
    store, root, workspace, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    job = enqueue(jobs, root)
    finish(jobs, job)
    directory = workspace / 'outputs/final-answer.md'
    directory.mkdir()
    (directory / 'artifact.txt').write_text('Must not be hidden')
    store.update(root, 'completed')
    assert acceptance.summary(root)['status'] == 'stale'


def test_partial_output_scope_cannot_establish_task_acceptance(tmp_path):
    store, root, workspace, jobs = setup_task(tmp_path)
    registry(store)
    (workspace / 'outputs/another-case.txt').write_text('another case')
    with pytest.raises(ValueError, match='outputs'):
        enqueue(jobs, root, paths=['outputs/result.txt'])
    assert jobs.claim() is None


def test_new_output_outside_explicit_selected_files_invalidates_acceptance(tmp_path):
    store, root, workspace, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    job = enqueue(jobs, root, paths=['outputs/result.txt'])
    finish(jobs, job)
    store.update(root, 'completed')
    assert acceptance.summary(root)['status'] == 'passed'
    (workspace / 'outputs/late-summary.md').write_text('new output outside selected paths')
    assert registry(TaskStore(tmp_path)).summary(root)['status'] == 'stale'


def test_nested_group_reviewer_cannot_run_task_acceptance(tmp_path):
    from app.tools import make_tools, task_context
    from dapr_agents.types.exceptions import ToolError
    store, root, _, jobs = setup_task(tmp_path)
    registry(store)
    store.register_flow('group', root, 'swarm_lead')
    store.register_flow('group-reviewer', 'group', 'swarm_reviewer')
    with pytest.raises(ValueError, match='корн'):
        enqueue(jobs, root, flow='group-reviewer')
    token = task_context.set((root, 'group-reviewer'))
    try:
        tool = next(tool for tool in make_tools(store) if tool.name == 'run_validation')
        with pytest.raises(ToolError, match='корн'):
            tool(command='true', artifact_paths=['outputs'], criteria=['All outputs checked'])
    finally:
        task_context.reset(token)
    assert jobs.claim() is None


def test_older_runtime_receipt_without_task_output_scope_is_not_task_acceptance(tmp_path):
    store, root, _, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    job = enqueue(jobs, root)
    finish(jobs, job)
    with store.connect() as db:
        row = db.execute('SELECT snapshot FROM acceptance_checks WHERE job_id=?', (job['id'],)).fetchone()
        old_snapshot = json.loads(row['snapshot'])
        old_snapshot.pop('task_outputs')
        db.execute('UPDATE acceptance_checks SET snapshot=? WHERE job_id=?', (json.dumps(old_snapshot), job['id']))
    store.update(root, 'completed')
    assert acceptance.summary(root)['status'] == 'stale'


def test_admin_import_still_supports_independently_verified_nested_reviewer(tmp_path):
    store, root, _, jobs = setup_task(tmp_path)
    acceptance = registry(store)
    store.register_flow('group', root, 'swarm_lead')
    store.register_flow('group-reviewer', 'group', 'swarm_reviewer')
    job = jobs.enqueue(root, 'group-reviewer', 'old-independent', 'true', kind='command')
    finish(jobs, job)
    store.update(root, 'completed')
    result = acceptance.import_verified_receipt(root, job['id'], {
        'outputs/result.txt': hashlib.sha256(b'answer').hexdigest()}, ['Independent full audit'])
    assert result['status'] == 'passed'
