"""Durable local queue between container agents and the Mac's Codex worker."""
import hashlib
import json
import re
import time

from app.store import now
from app.acceptance import AcceptanceStore, validation_request

MODEL = 'gpt-6-luna'
MAX_JOBS = 10


class CodexJobs:
    def __init__(self, store):
        self.store = store
        self.acceptance = AcceptanceStore(store)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS codex_jobs (
                id TEXT PRIMARY KEY, root TEXT NOT NULL, flow TEXT NOT NULL,
                prompt TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                created TEXT NOT NULL, pid INTEGER)''')
            if 'kind' not in {row['name'] for row in db.execute('PRAGMA table_info(codex_jobs)')}:
                db.execute("ALTER TABLE codex_jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'codex'")

    def enqueue(self, root, flow, call_id, prompt, kind='codex', validation=None):
        if validation is not None:
            if kind != 'command':
                raise ValueError('Приёмка требует локальной команды без модели.')
            validation = validation_request(validation)
        if kind not in ('codex', 'command'):
            raise ValueError('Неизвестный исполнитель.')
        if self.store.root_for(flow) != root:
            raise ValueError('Codex: workflow относится к другой задаче.')
        if not re.fullmatch(r'[\w.:-]{1,200}', call_id or '') or not 1 <= len(prompt.strip()) <= 16000:
            raise ValueError('Codex: некорректный запрос.')
        job_id = hashlib.sha256(f'{root}:{flow}:{call_id}'.encode()).hexdigest()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if validation is not None:
                self.acceptance.require_reviewer(db, root, flow)
            task = db.execute('SELECT * FROM tasks WHERE id=?', (root,)).fetchone()
            if task['stopped'] or task['state'] != 'running':
                raise RuntimeError('Задача остановлена или не выполняется.')
            if kind == 'codex' and not task['allow_codex']:
                raise RuntimeError('Codex выключен для этой задачи. Пишите код через write_file и проверяйте через run_command, используя GLM.')
            existing = db.execute('SELECT * FROM codex_jobs WHERE id=?', (job_id,)).fetchone()
            if existing:
                receipt = db.execute('SELECT request FROM acceptance_checks WHERE job_id=?', (job_id,)).fetchone()
                if (json.loads(receipt['request']) if receipt else None) != validation:
                    raise ValueError('Идентификатор вызова уже использован для другой проверки.')
                if existing['prompt'] != prompt or existing['kind'] != kind:
                    raise ValueError('Codex: идентификатор вызова уже использован для другого задания.')
                return dict(existing)
            count = db.execute('SELECT count(*) FROM codex_jobs WHERE root=? AND kind=?', (root, kind)).fetchone()[0]
            limit = min(MAX_JOBS, task['agent_limit']) if kind == 'codex' else task['command_limit']
            if count >= limit:
                raise RuntimeError('Достигнут лимит локальных запусков для задачи.')
            if kind == 'command':
                role = db.execute('SELECT agent FROM flows WHERE id=?',(flow,)).fetchone()[0]
                work_used = db.execute("SELECT count(*) FROM codex_jobs j JOIN flows f ON f.id=j.flow WHERE j.root=? AND j.kind='command' AND f.agent!='swarm_reviewer'",(root,)).fetchone()[0]
                if role != 'swarm_reviewer' and work_used >= limit - task['command_reserve']:
                    raise RuntimeError('Рабочий бюджет команд исчерпан. Оставшиеся команды зарезервированы для swarm_reviewer; передайте ему проверку.')
            db.execute('INSERT INTO codex_jobs(id,root,flow,prompt,created,kind) VALUES(?,?,?,?,?,?)',
                       (job_id, root, flow, prompt, now(), kind))
            if validation is not None:
                self.acceptance.enqueue(db, root, flow, job_id, validation)
        label = f'Codex · {MODEL}' if kind == 'codex' else 'Команда GLM'
        self.store.event(root, flow, kind, f'{label}: задание в очереди')
        return self.get(job_id)

    def get(self, job_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM codex_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def list(self, root):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT id,state,result,error,created,kind FROM codex_jobs WHERE root=? ORDER BY created', (root,))]

    def budget(self, root):
        with self.store.connect() as db:
            task = db.execute('SELECT command_limit,command_reserve FROM tasks WHERE id=?',(root,)).fetchone()
            used = db.execute("SELECT count(*) FROM codex_jobs WHERE root=? AND kind='command'",(root,)).fetchone()[0]
            work = db.execute("SELECT count(*) FROM codex_jobs j JOIN flows f ON f.id=j.flow WHERE j.root=? AND j.kind='command' AND f.agent!='swarm_reviewer'",(root,)).fetchone()[0]
        remaining = max(0,task['command_limit']-used)
        return dict(limit=task['command_limit'],reserve=task['command_reserve'],used=used,remaining=remaining,
                    work_remaining=max(0,min(remaining,task['command_limit']-task['command_reserve']-work)),
                    review_remaining=remaining)

    def claim(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE codex_jobs SET state='stopped' WHERE state='queued' AND root IN (SELECT id FROM tasks WHERE stopped=1)")
            db.execute("UPDATE codex_jobs SET state='failed',error='Codex выключен для этой задачи.' WHERE state='queued' AND kind='codex' AND root IN (SELECT id FROM tasks WHERE allow_codex=0)")
            row = db.execute("SELECT j.* FROM codex_jobs j JOIN tasks t ON t.id=j.root WHERE j.state='queued' AND t.state='running' AND t.stopped=0 ORDER BY j.created LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE codex_jobs SET state='running' WHERE id=?", (row['id'],))
            return dict(row)

    def finish(self, job_id, state, result='', error=''):
        with self.store.connect() as db:
            db.execute('UPDATE codex_jobs SET state=?,result=?,error=?,pid=NULL WHERE id=?',
                       (state, result, error, job_id))
            self.acceptance.finish(db, job_id, state, result)

    def set_pid(self, job_id, pid):
        with self.store.connect() as db:
            db.execute('UPDATE codex_jobs SET pid=? WHERE id=?', (pid, job_id))

    def interrupted(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM codex_jobs WHERE state='running'")]


def worker_status(store):
    try:
        data = json.loads((store.directory / 'codex-worker.json').read_text())
        return {'ready': time.time() - data['heartbeat'] < 15, 'model': MODEL, 'limit': MAX_JOBS}
    except (OSError, ValueError, KeyError):
        return {'ready': False, 'model': MODEL, 'limit': MAX_JOBS}
