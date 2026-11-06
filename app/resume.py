"""Continue interrupted work in a new, linked workspace; never replay old commands."""
import json
import os
from pathlib import Path
import shutil
import stat
import uuid

from app.store import now

SNAPSHOT_LIMIT = 200 * 1024 * 1024
FILE_COUNT_LIMIT = 2000


class ResumeConflict(RuntimeError):
    pass


def snapshot(source, target):
    if source.is_symlink():
        raise ValueError('Папка исходной задачи не может быть ссылкой.')
    total, count = 0, 0
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        if any(part.startswith('.') or part == '__pycache__' for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError('Возобновление невозможно: среди файлов есть символическая ссылка.')
        if path.is_dir():
            continue
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as original:
            mode = os.fstat(original.fileno()).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError('Сохранять можно только обычные файлы.')
            count += 1
            if count > FILE_COUNT_LIMIT:
                raise ValueError('Для возобновления допускается не больше 2000 файлов.')
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('xb') as output:
                while block := original.read(65536):
                    total += len(block)
                    if total > SNAPSHOT_LIMIT:
                        raise ValueError('Сохранённые файлы превышают 200 MiB.')
                    output.write(block)
                os.fchmod(output.fileno(), stat.S_IMODE(mode) & 0o777)


def continue_task(store, task_id, automatic=False):
    def eligible(source):
        if automatic:
            import time
            from app.recovery import DELAYS
            if (not source['auto_recover'] or source['stop_requested'] or source['state'] != 'failed'
                    or source['recovery_status'] != 'scheduled' or source['recovery_after'] is None
                    or source['recovery_after'] > time.time() or source['recovery_attempt'] >= len(DELAYS)):
                raise ResumeConflict('Автоматическое продолжение отменено или ещё не наступило время попытки.')

    # Fence old model/tool activities before waiting for any host process to exit.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        source = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if source is None:
            raise KeyError(task_id)
        if source['resumed_as']:
            return dict(db.execute('SELECT * FROM tasks WHERE id=?', (source['resumed_as'],)).fetchone())
        stop_generation = source['stop_generation']
        eligible(source)
        if source['state'] not in ('failed', 'stopped'):
            raise ResumeConflict('Возобновить можно задачу с ошибкой или остановленную задачу.')
        db.execute('UPDATE tasks SET stopped=1 WHERE id=?', (task_id,))
        db.execute("UPDATE codex_jobs SET state='stopped' WHERE root=? AND state='queued'", (task_id,))

    new_id = uuid.uuid4().hex
    destination = store.workspace(new_id)
    created = False
    committed = False
    try:
        # Bounded local snapshot under the transaction: duplicate clicks cannot
        # create two continuations and no incomplete copy becomes runnable.
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            source = dict(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
            if source['resumed_as']:
                return dict(db.execute('SELECT * FROM tasks WHERE id=?', (source['resumed_as'],)).fetchone())
            if source['stop_generation'] != stop_generation:
                raise ResumeConflict('Остановка отменила начатое возобновление.')
            eligible(source)
            from app.recovery import remaining_policy
            policy = remaining_policy(db,source) if automatic else {name:source[name] for name in ('agent_limit','llm_limit','command_limit','command_reserve')}
            if automatic and min(policy['agent_limit'],policy['llm_limit'],policy['command_limit']) < 1:
                raise ValueError('Исчерпан оставшийся бюджет автоматического продолжения.')
            if db.execute("SELECT 1 FROM codex_jobs WHERE root=? AND state='running'", (task_id,)).fetchone():
                raise ResumeConflict('Исполнитель завершает остановку. Повторите возобновление через несколько секунд.')
            events = [dict(row) for row in db.execute(
                'SELECT kind,content,created FROM events WHERE root=? ORDER BY id DESC LIMIT 30', (task_id,))][::-1]
            for event in events:
                event['content'] = event['content'][:500]
            jobs = [dict(row) for row in db.execute(
                'SELECT id,kind,state,error,result FROM codex_jobs WHERE root=? ORDER BY created DESC LIMIT 10', (task_id,))]
            for job in jobs:
                job['result'] = job['result'][:200]
                job['error'] = job['error'][:200]
            destination.mkdir()
            created = True
            snapshot(store.workspace(task_id), destination)
            notes = destination / 'notes'
            notes.mkdir(exist_ok=True)
            context = {'source_id': task_id, 'state': source['state'], 'error': source['error'],
                       'previous_result': source['result'][:4000], 'events': events, 'jobs': jobs, 'automatic':automatic,
                       'remaining_policy':policy,
                       'notice': 'Partial history, not full transcript. Check existing artifacts before repeating work.'}
            (notes / f'resume-{task_id}.json').write_text(json.dumps(context, ensure_ascii=False), encoding='utf-8')
            db.execute('INSERT INTO tasks(id,prompt,created,state,agent_limit,llm_limit,allow_codex,resumed_from,command_limit,command_reserve,work_window,auto_recover,recovery_attempt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (new_id, source['prompt'], now(), 'queued', policy['agent_limit'], policy['llm_limit'], 0, task_id,
                 policy['command_limit'],policy['command_reserve'],source['work_window'],1,source['recovery_attempt']+1 if automatic else 0))
            db.execute('INSERT INTO flows(id,root,parent,agent) VALUES(?,?,NULL,?)', (new_id, new_id, 'coordinator'))
            db.execute('UPDATE tasks SET resumed_as=?,recovery_status=?,recovery_after=NULL WHERE id=?', (new_id, 'continued' if automatic else 'idle',task_id))
            for root, message in [(task_id, f'Продолжение задачи: {new_id}.'),
                                  (new_id, f'Продолжение {task_id}. Файлы и недавний журнал сохранены. Лимиты нового запуска: {policy["agent_limit"]} агентов, {policy["llm_limit"]} вызовов.')]:
                db.execute('INSERT INTO events(root,flow,kind,content,created) VALUES(?,?,?,?,?)',
                           (root, root, 'resume', message, now()))
        committed = True
        return store.get(new_id)
    except Exception:
        if created and not committed:
            shutil.rmtree(destination)
        raise
