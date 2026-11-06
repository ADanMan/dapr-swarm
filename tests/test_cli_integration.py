"""Black-box acceptance: real CLI process and real FastAPI HTTP boundary."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'app' / 'cli.py'


@pytest.fixture
def local_api(tmp_path):
    from app.main import create_app
    from app.store import TaskStore
    store = TaskStore(tmp_path / 'server')
    app = create_app(store, runtime_enabled=False)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    until = time.monotonic() + 5
    while not server.started:
        assert time.monotonic() < until, 'test HTTP server did not start'
        time.sleep(0.01)
    yield store, f'http://127.0.0.1:{port}'
    server.should_exit = True
    thread.join(timeout=5)
    listener.close()


def cli(url, *args, json_mode=True):
    assert CLI.is_file(), 'CLI implementation is not installed yet'
    command = [sys.executable, str(CLI), '--base-url', url]
    if json_mode:
        command.append('--json')
    return subprocess.run(command + list(args), capture_output=True, text=True, timeout=10,
                          env={**os.environ, 'HTTP_PROXY':'http://127.0.0.1:1', 'http_proxy':'http://127.0.0.1:1', 'NO_PROXY':'', 'no_proxy':''})


def test_cli_real_api_submit_upload_result_download_stop(local_api, tmp_path):
    store, url = local_api
    upload = tmp_path / 'материал.txt'
    upload.write_text('synthetic attachment: 42', encoding='utf-8')
    health = cli(url, 'health')
    assert json.loads(health.stdout)['model'] == 'glm-5.3-flash-uncensored-df'
    submitted = cli(url, 'submit', 'Проверь файлы', '--file', str(upload), '--agents', '4', '--calls', '30')
    assert submitted.returncode == 0, submitted.stderr
    task = json.loads(submitted.stdout)
    assert task['allow_codex'] == 0
    assert task['agent_limit'] == 4 and task['llm_limit'] == 30
    assert (store.workspace(task['id']) / 'inputs' / upload.name).read_bytes() == upload.read_bytes()
    assert any(t['id'] == task['id'] for t in json.loads(cli(url, 'list').stdout))
    store.event(task['id'], task['id'], 'message', 'Проверено')
    assert 'Проверено' in cli(url, 'events', task['id'], json_mode=False).stdout
    output = store.workspace(task['id']) / 'outputs' / 'результат.txt'
    output.parent.mkdir()
    output.write_text('result bytes\n', encoding='utf-8')
    store.update(task['id'], 'completed', result='Готово')
    assert json.loads(cli(url, 'show', task['id']).stdout)['result'] == 'Готово'
    waited = cli(url, 'wait', task['id'], '--deadline', '2', '--interval', '0.05')
    assert waited.returncode == 0, waited.stderr
    assert json.loads(waited.stdout)['state'] == 'completed'
    destination = tmp_path / 'download.txt'
    downloaded = cli(url, 'download', task['id'], 'outputs/результат.txt', '--output', str(destination))
    assert downloaded.returncode == 0, downloaded.stderr
    assert destination.read_bytes() == output.read_bytes()
    assert cli(url, 'download', task['id'], 'outputs/результат.txt', '--output', str(destination)).returncode != 0
    assert destination.read_bytes() == output.read_bytes()
    stopped = cli(url, 'stop', task['id'])
    assert json.loads(stopped.stdout)['state'] == 'stopped'
    assert cli(url, 'wait', task['id'], '--deadline', '1').returncode == 1


def test_cli_timeout_validation_and_no_unintended_writes(local_api, tmp_path):
    store, url = local_api
    task = store.create('Pending', 2, 5)
    store.update(task['id'], 'queued')
    timed = cli(url, 'wait', task['id'], '--deadline', '0.15', '--interval', '0.05')
    assert timed.returncode == 3, timed.stderr
    assert store.get(task['id'])['state'] == 'queued'
    invalid = cli(url, 'submit', ' ', '--agents', '1')
    assert invalid.returncode != 0 and not invalid.stdout.strip()
    assert len(store.list()) == 1
    missing = cli(url, 'show', '0' * 32)
    assert missing.returncode != 0 and 'Traceback' not in missing.stderr
    assert cli(url, 'show', '../health').returncode != 0
    dest = tmp_path / 'not-written'
    assert cli(url, 'download', task['id'], '../tasks.sqlite', '--output', str(dest)).returncode != 0
    assert not dest.exists()
    assert cli('https://example.com', 'health').returncode != 0


def test_cli_symlink_download_does_not_overwrite(local_api, tmp_path):
    store, url = local_api
    task = store.create('Files', 2, 5)
    source = store.workspace(task['id']) / 'outputs' / 'a.txt'
    source.parent.mkdir()
    source.write_text('new')
    target = tmp_path / 'keep.txt'
    target.write_text('keep')
    link = tmp_path / 'link.txt'
    link.symlink_to(target)
    result = cli(url, 'download', task['id'], 'outputs/a.txt', '--output', str(link))
    assert result.returncode != 0
    assert target.read_text() == 'keep' and link.is_symlink()


def test_cli_interrupt_wait_does_not_stop_task(local_api):
    import signal
    store, url = local_api
    task = store.create('Keep running after interrupt', 2, 5)
    store.update(task['id'], 'running')
    assert CLI.is_file(), 'CLI implementation is not installed yet'
    process = subprocess.Popen([sys.executable, str(CLI), '--base-url', url, 'wait', task['id'], '--interval', '0.05'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(0.35)
        process.send_signal(signal.SIGINT)
        out, error = process.communicate(timeout=3)
        assert process.returncode == 130, error
        assert 'Traceback' not in error
        assert store.get(task['id'])['state'] == 'running'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


@pytest.fixture
def hostile_server():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        mode = 'json'
        requests = []
        command_budget = None

        def log_message(self, *_):
            pass

        def do_POST(self):
            self.requests.append(self.path)
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.close_connection = True

        def do_GET(self):
            self.requests.append(self.path)
            if self.mode == 'redirect':
                self.send_response(302)
                self.send_header('Location', '/redirect-target')
                self.end_headers()
                return
            self.send_response(200)
            if self.mode == 'slow':
                body = json.dumps({'id':'a'*32, 'state':'completed'}).encode()
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    for offset in range(0, len(body), 10):
                        self.wfile.write(body[offset:offset+10])
                        self.wfile.flush()
                        time.sleep(0.07)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if self.mode == 'partial':
                self.send_header('Content-Length', '100')
                self.end_headers()
                self.wfile.write(b'short')
                return
            self.end_headers()
            body = b'not-json' if self.mode == 'invalid' else b'x'*(8*1024*1024+1) if self.mode == 'large' else json.dumps({
                'id':'a'*32,'state':'completed','prompt':'control\x1b[2J','result':'danger\x1b]52;c;AAAA\x07','llm_calls':1,
                'error':'','files':[],'agents':[],'events':[], 'command_budget': self.command_budget}).encode()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Handler, f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def test_cli_does_not_follow_redirect_or_accept_broken_json(hostile_server):
    handler, url = hostile_server
    handler.mode = 'redirect'
    result = cli(url, 'health')
    assert result.returncode != 0 and not result.stdout.strip()
    assert handler.requests == ['/api/health']
    for mode in ['invalid', 'large']:
        handler.mode = mode
        result = cli(url, 'health')
        assert result.returncode != 0 and not result.stdout.strip()
        assert 'Traceback' not in result.stderr


def test_cli_partial_download_is_never_published(hostile_server, tmp_path):
    handler, url = hostile_server
    handler.mode = 'partial'
    destination = tmp_path / 'result.bin'
    result = cli(url, 'download', 'a'*32, 'outputs/x.bin', '--output', str(destination))
    assert result.returncode != 0
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_cli_sanitizes_terminal_controls(hostile_server):
    _, url = hostile_server
    result = cli(url, 'show', 'a'*32, json_mode=False)
    assert result.returncode == 0, result.stderr
    assert '\x1b' not in result.stdout and '\x07' not in result.stdout


def test_cli_never_retries_uncertain_submission(hostile_server):
    handler, url = hostile_server
    result = cli(url, 'submit', 'One task only')
    assert result.returncode != 0 and 'Traceback' not in result.stderr
    assert handler.requests == ['/api/tasks']


@pytest.mark.parametrize('option', ['--timeout', '--interval', '--deadline'])
@pytest.mark.parametrize('value', ['nan', 'inf', '-1', '0'])
def test_cli_rejects_nonpositive_or_nonfinite_times(local_api, option, value):
    store, url = local_api
    task = store.create('No infinite waits', 2, 5)
    args = (option, value, 'health') if option == '--timeout' else ('wait', task['id'], option, value)
    result = cli(url, *args)
    assert result.returncode != 0
    assert 'Traceback' not in result.stderr


def test_cli_validates_uploads_before_submission(local_api, tmp_path):
    store, url = local_api
    one = tmp_path / 'one' / 'same.txt'
    two = tmp_path / 'two' / 'same.txt'
    for path in (one, two):
        path.parent.mkdir()
        path.write_text('duplicate')
    assert cli(url, 'submit', 'Task', '--file', str(one), '--file', str(two)).returncode != 0
    unsafe = tmp_path / 'bad"name.txt'
    unsafe.write_text('no header injection')
    assert cli(url, 'submit', 'Task', '--file', str(unsafe)).returncode != 0
    oversized = tmp_path / 'large.bin'
    with oversized.open('wb') as file:
        file.truncate(10 * 1024 * 1024 + 1)
    assert cli(url, 'submit', 'Task', '--file', str(oversized)).returncode != 0
    assert store.list() == []


def test_cli_wait_deadline_bounds_slow_response(hostile_server):
    handler, url = hostile_server
    handler.mode = 'slow'
    started = time.monotonic()
    result = cli(url, 'wait', 'a'*32, '--deadline', '0.1')
    elapsed = time.monotonic() - started
    assert result.returncode == 3, (elapsed, result.stdout, result.stderr)
    assert not result.stdout.strip()
    assert elapsed < 0.35, elapsed


def test_cli_resume_preserves_files_and_links_runs(local_api):
    store, url = local_api
    submitted = cli(url, 'submit', 'Continue from saved output', '--agents', '2', '--calls', '10',
                    '--commands', '42', '--command-reserve', '7', '--work-window', '3')
    assert submitted.returncode == 0, submitted.stderr
    original = json.loads(submitted.stdout)
    output = store.workspace(original['id'])/'outputs'
    output.mkdir()
    (output/'checkpoint.txt').write_text('keep this')
    store.stop(original['id'])
    result = cli(url, 'resume', original['id'])
    assert result.returncode == 0, result.stderr
    continuation = json.loads(result.stdout)
    assert continuation['resumed_from'] == original['id']
    assert (continuation['command_limit'], continuation['command_reserve'], continuation['work_window']) == (42, 7, 3)
    assert (store.workspace(continuation['id'])/'outputs/checkpoint.txt').read_text() == 'keep this'
    again = cli(url, 'resume', original['id'])
    assert json.loads(again.stdout)['id'] == continuation['id']


def test_cli_new_default_budgets(local_api):
    _, url = local_api
    result = cli(url, 'submit', 'Default CLI budget')
    assert result.returncode == 0, result.stderr
    task = json.loads(result.stdout)
    assert (task['agent_limit'], task['llm_limit']) == (2000, 20000)
    assert (task['command_limit'], task['command_reserve'], task['work_window']) == (1000, 100, 16)
    assert task['allow_codex'] == 0


@pytest.mark.parametrize('commands,reserve,window', [(1, 0, 1), (20000, 19999, 64), (300, 30, 8)])
def test_cli_submit_command_budget_boundaries(local_api, commands, reserve, window):
    _, url = local_api
    result = cli(url, 'submit', 'Budget boundary', '--commands', str(commands),
                 '--command-reserve', str(reserve), '--work-window', str(window))
    assert result.returncode == 0, result.stderr
    task = json.loads(result.stdout)
    assert (task['command_limit'], task['command_reserve'], task['work_window']) == (commands, reserve, window)
    assert task['allow_codex'] == 0


@pytest.mark.parametrize('options', [
    ('--commands', '0'), ('--commands', '20001'), ('--commands', '1.5'),
    ('--commands', 'nan'), ('--commands', '100'),
    ('--command-reserve', '-1'), ('--command-reserve', '1000'), ('--command-reserve', '1.5'),
    ('--commands', '5', '--command-reserve', '5'),
    ('--commands', '5', '--command-reserve', '6'),
    ('--work-window', '0'), ('--work-window', '65'), ('--work-window', '1.5'),
])
def test_cli_invalid_command_budgets_never_reach_http(hostile_server, options):
    handler, url = hostile_server
    result = cli(url, 'submit', 'Invalid budget', *options)
    assert result.returncode != 0
    assert not result.stdout.strip() and 'Traceback' not in result.stderr
    assert handler.requests == []


def test_cli_show_human_command_budget(hostile_server):
    handler, url = hostile_server
    handler.command_budget = {'limit': 300, 'reserve': 30, 'used': 42, 'remaining': 258,
                              'work_remaining': 228, 'review_remaining': 30}
    result = cli(url, 'show', 'a' * 32, json_mode=False)
    assert result.returncode == 0, result.stderr
    assert 'Команды: 42/300; осталось: 258' in result.stdout
    assert 'работа: 228; проверка: 30; резерв: 30' in result.stdout


def test_cli_wait_follows_automatic_continuation_without_post(local_api):
    from app.recovery import record_model_failure,RecoveryManager
    from app.resume import continue_task
    store,url=local_api
    task=store.create('Recover and finish',5,30);root=task['id'];store.update(root,'running')
    marker=record_model_failure(store,root,root,'length','truncated')
    store.update(root,'failed',error=marker);RecoveryManager(store).schedule(root,marker)
    def finish():
        time.sleep(.25)
        with store.connect() as db:db.execute('UPDATE tasks SET recovery_after=0 WHERE id=?',(root,))
        child=continue_task(store,root,automatic=True)
        store.update(child['id'],'completed',result='Recovered')
    thread=threading.Thread(target=finish);thread.start()
    try:
        result=cli(url,'wait',root,'--deadline','3','--interval','0.03')
        assert result.returncode==0,result.stderr
        data=json.loads(result.stdout)
        assert data['resumed_from']==root and data['result']=='Recovered'
        assert len(store.list())==2
    finally:thread.join(5)


def test_cli_wait_deadline_includes_scheduled_recovery(local_api):
    from app.recovery import record_model_failure,RecoveryManager
    store,url=local_api
    task=store.create('Temporarily unavailable',5,30);root=task['id'];store.update(root,'running')
    marker=record_model_failure(store,root,root,'timeout','timeout')
    store.update(root,'failed',error=marker);RecoveryManager(store).schedule(root,marker)
    result=cli(url,'wait',root,'--deadline','0.2','--interval','0.03')
    assert result.returncode==3
    assert store.get(root)['stop_requested']==0 and store.get(root)['resumed_as'] is None
