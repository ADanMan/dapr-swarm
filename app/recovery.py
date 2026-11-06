"""Durable, bounded continuation for proven transient model failures."""
from datetime import datetime, timezone
import json
import re
import time
import uuid

from app.store import now

DELAYS = (15, 60, 180)
KINDS = frozenset({'length','empty','upstream_auth','timeout','connection','rate_limit','server'})
MARKER = 'SWARM_RECOVERABLE:'


def record_model_failure(store, root, flow, kind, message):
    if kind not in KINDS:
        raise ValueError('Unknown model failure category')
    token = uuid.uuid4().hex
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT stopped,stop_requested FROM tasks WHERE id=?',(root,)).fetchone()
        if row is None or row['stopped'] or row['stop_requested']:
            return ''
        if store.root_for(flow) != root:
            raise ValueError('Failure belongs to another task')
        db.execute('INSERT INTO model_failures VALUES(?,?,?,?,?,?)',
                   (token,root,flow,kind,message[:1400],now()))
        db.execute('INSERT INTO events(root,flow,kind,content,created) VALUES(?,?,?,?,?)',
                   (root,flow,'model_failure',json.dumps({'kind':kind,'message':message[:1400]},ensure_ascii=False),now()))
    return MARKER+token


def recovery_view(task):
    enabled = bool(task.get('auto_recover')) and not task.get('stop_requested')
    after = task.get('recovery_after')
    return dict(enabled=enabled,status=task.get('recovery_status','idle') if enabled else 'disabled',
                attempt=task.get('recovery_attempt',0),max_attempts=len(DELAYS),
                retry_at=datetime.fromtimestamp(after,timezone.utc).isoformat() if after is not None else None,
                reason=task.get('recovery_reason',''),next_task_id=task.get('resumed_as'))


def remaining_policy(db, task):
    root = task['id']
    children = db.execute('SELECT count(*) FROM flows WHERE root=? AND parent IS NOT NULL',(root,)).fetchone()[0]
    used = db.execute("SELECT count(*) FROM codex_jobs WHERE root=? AND kind='command'",(root,)).fetchone()[0]
    work = db.execute("SELECT count(*) FROM codex_jobs j JOIN flows f ON f.id=j.flow WHERE j.root=? AND j.kind='command' AND f.agent!='swarm_reviewer'",(root,)).fetchone()[0]
    commands = max(0,task['command_limit']-used)
    work_remaining = max(0,min(commands,task['command_limit']-task['command_reserve']-work))
    return dict(agent_limit=max(0,task['agent_limit']-children),llm_limit=max(0,task['llm_limit']-task['llm_calls']),
                command_limit=commands,command_reserve=commands-work_remaining)


class RecoveryManager:
    def __init__(self, store):
        self.store = store
        from app.codex_jobs import CodexJobs
        CodexJobs(store)

    def schedule(self, root, error):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = db.execute('SELECT * FROM tasks WHERE id=?',(root,)).fetchone()
            if task is None or task['state']!='failed' or not task['auto_recover'] or task['stop_requested'] or task['resumed_as']:
                return
            if task['recovery_status']=='scheduled':
                return
            tokens = re.findall(r'SWARM_RECOVERABLE:([a-f0-9]{32})',error)
            failure = next((row for token in tokens if (row := db.execute(
                'SELECT * FROM model_failures WHERE token=? AND root=?',(token,root)).fetchone())),None)
            status, reason, after = 'blocked','Ошибка не относится к подтверждённому временному сбою модели.',None
            if failure and failure['kind'] in KINDS:
                policy = remaining_policy(db,task)
                if min(policy['agent_limit'],policy['llm_limit'],policy['command_limit'])<1:
                    reason='Автопродолжение невозможно: исчерпан общий бюджет агентов, вызовов или команд.'
                elif task['recovery_attempt']>=len(DELAYS):
                    status,reason='exhausted','Исчерпаны три автоматических продолжения. Промежуточные файлы сохранены.'
                else:
                    status,reason,after='scheduled',failure['message'],time.time()+DELAYS[task['recovery_attempt']]
            db.execute('UPDATE tasks SET recovery_status=?,recovery_reason=?,recovery_after=? WHERE id=?',(status,reason,after,root))
            db.execute('INSERT INTO events(root,flow,kind,content,created) VALUES(?,?,?,?,?)',
                       (root,root,'recovery',json.dumps({'status':status,'reason':reason,'retry_after_seconds':None if after is None else DELAYS[task['recovery_attempt']]},ensure_ascii=False),now()))

    def process(self):
        from app.resume import continue_task, ResumeConflict
        # A crash may land after persisting failed but before scheduling its continuation.
        with self.store.connect() as db:
            unfinished = [dict(row) for row in db.execute("SELECT * FROM tasks WHERE state='failed' AND auto_recover=1 AND stop_requested=0 AND resumed_as IS NULL AND recovery_status='idle'")]
        for task in unfinished:
            self.schedule(task['id'],task['error'])
        with self.store.connect() as db:
            tasks = [dict(row) for row in db.execute("SELECT * FROM tasks WHERE recovery_status='scheduled' AND stop_requested=0 AND resumed_as IS NULL AND recovery_after<=?",(time.time(),))]
        for task in tasks:
            root = task['id']
            try:
                continue_task(self.store,root,automatic=True)
            except ResumeConflict as error:
                with self.store.connect() as db:
                    # A fenced worker has a bounded window to acknowledge cancellation.
                    current = db.execute('SELECT * FROM tasks WHERE id=?',(root,)).fetchone()
                    if current['stop_requested'] or current['resumed_as'] or current['recovery_status']!='scheduled':
                        continue
                    started = current['recovery_wait_since'] or time.time()
                    blocked = time.time()-started>=180
                    db.execute("UPDATE tasks SET recovery_wait_since=?,recovery_after=?,recovery_status=?,recovery_reason=? WHERE id=? AND resumed_as IS NULL AND stop_requested=0 AND recovery_status='scheduled'",
                               (started,None if blocked else time.time()+2,'blocked' if blocked else 'scheduled',str(error)[:1400],root))
            except (OSError,ValueError) as error:
                with self.store.connect() as db:
                    db.execute("UPDATE tasks SET recovery_status='blocked',recovery_after=NULL,recovery_reason=? WHERE id=? AND resumed_as IS NULL AND stop_requested=0",('Не удалось сохранить продолжение: '+str(error)[:1000],root))
