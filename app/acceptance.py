"""Durable command-backed acceptance with bounded, no-symlink artifact snapshots."""
import hashlib
import json
import os
from pathlib import PurePosixPath
import stat

from app.store import now

MAX_ARTIFACTS = 4096
MAX_ENTRIES = 8192
MAX_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024


def validation_request(value):
    if not isinstance(value, dict) or set(value) != {'artifact_paths', 'criteria'}:
        raise ValueError('Укажите artifact_paths и criteria проверки.')
    paths, criteria = value['artifact_paths'], value['criteria']
    if not isinstance(paths, list) or not 1 <= len(paths) <= 256:
        raise ValueError('Укажите 1–256 путей артефактов.')
    normalized = []
    for path in paths:
        if not isinstance(path, str) or not path or len(path) > 1024 or '\x00' in path:
            raise ValueError('Некорректный путь артефакта.')
        relative = PurePosixPath(path)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Артефакты должны находиться внутри папки задачи.')
        if relative.parts[:2] == ('outputs', '.codex-tmp'):
            raise ValueError('Служебный каталог исполнителя не является артефактом.')
        normalized.append(str(relative))
    if (not isinstance(criteria, list) or not 1 <= len(criteria) <= 100 or
            any(not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in criteria)):
        raise ValueError('Укажите 1–100 непустых критериев проверки.')
    return {'artifact_paths': sorted(set(normalized)), 'criteria': criteria}


def snapshot(workspace, paths, *, task_outputs=False, require_coverage=False):
    """Hash selected artifacts without links; directory walks omit runtime final-answer.md.

    Explicitly selecting outputs/final-answer.md still hashes it. The worker scratch
    subtree is always excluded, as documented by run_validation. Task acceptance
    also snapshots all outputs in the same map, hashing each shared file once.
    """
    files, directories = {}, set()
    entries = total = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def visit(parent_fd, name, relative, selected=False):
        nonlocal entries, total
        # The unchanged worker owns this temporary subtree; never certify it.
        if relative == 'outputs/.codex-tmp':
            return
        entries += 1
        if entries > MAX_ENTRIES:
            raise ValueError('Слишком много элементов в снимке артефактов.')
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                if relative in directories:
                    return
                directories.add(relative)
                with os.scandir(fd) as children:
                    for child in children:
                        visit(fd, child.name, str(PurePosixPath(relative) / child.name))
            elif stat.S_ISREG(info.st_mode):
                # Engine writes this summary only after the root workflow completes.
                if relative == 'outputs/final-answer.md' and not selected:
                    return
                if relative in files:
                    return
                if len(files) >= MAX_ARTIFACTS or info.st_size > MAX_FILE_BYTES:
                    raise ValueError('Превышен лимит размера или числа артефактов.')
                digest, size = hashlib.sha256(), 0
                while chunk := os.read(fd, 1024 * 1024):
                    total += len(chunk)
                    size += len(chunk)
                    if total > MAX_BYTES or size > MAX_FILE_BYTES:
                        raise ValueError('Превышен лимит размера снимка артефактов.')
                    digest.update(chunk)
                after = os.fstat(fd)
                if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError('Артефакт изменился во время чтения.')
                files[relative] = digest.hexdigest()
            else:
                raise ValueError('Артефакт должен быть обычным файлом или каталогом.')
        finally:
            os.close(fd)

    try:
        root_fd = os.open(workspace, directory_flags)
        try:
            for path in paths:
                parts = PurePosixPath(path).parts or ('.',)
                parent_fd = os.dup(root_fd)
                try:
                    for part in parts[:-1]:
                        next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                        os.close(parent_fd)
                        parent_fd = next_fd
                    visit(parent_fd, parts[-1], path, selected=True)
                finally:
                    os.close(parent_fd)
            if task_outputs:
                selected_files = set(files)
                visit(root_fd, 'outputs', 'outputs')
                if require_coverage and set(files) - selected_files:
                    raise ValueError('Приёмка задачи должна включать все обычные файлы outputs. Используйте artifact_paths=["outputs"].')
        finally:
            os.close(root_fd)
    except OSError as error:
        raise ValueError('Артефакт отсутствует, недоступен или содержит символическую ссылку.') from error
    if not files:
        raise ValueError('Снимок должен содержать хотя бы один обычный файл.')
    evidence = {'files': dict(sorted(files.items())), 'directories': sorted(directories)}
    if task_outputs:
        evidence['task_outputs'] = True
    return evidence


def exit_code(result):
    try:
        payload = json.loads(result)
        code = payload.get('exit_code') if isinstance(payload, dict) else None
        return code if type(code) is int else None
    except (ValueError, TypeError):
        return None


class AcceptanceStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS acceptance_checks (
                job_id TEXT PRIMARY KEY, root TEXT NOT NULL, request TEXT NOT NULL,
                snapshot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'checking',
                checked_at TEXT, exit_code INTEGER, source TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '')''')

    @staticmethod
    def require_reviewer(db, root, flow, *, task_level=True):
        row = db.execute('SELECT root,parent,agent FROM flows WHERE id=?', (flow,)).fetchone()
        if row is None or row['root'] != root or row['agent'] != 'swarm_reviewer':
            raise ValueError('Приёмку может запускать только swarm_reviewer этой задачи.')
        if task_level and row['parent'] != root:
            raise ValueError('Приёмку всей задачи запускает только swarm_reviewer — прямой потомок корня.')

    def enqueue(self, db, root, flow, job_id, request):
        self.require_reviewer(db, root, flow)
        evidence = snapshot(self.store.workspace(root), request['artifact_paths'], task_outputs=True, require_coverage=True)
        db.execute('INSERT INTO acceptance_checks(job_id,root,request,snapshot,source) VALUES(?,?,?,?,?)',
                   (job_id, root, json.dumps(request), json.dumps(evidence), 'runtime_validation'))

    def finish(self, db, job_id, state, result):
        row = db.execute('SELECT * FROM acceptance_checks WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            return
        # A repeated completion must not erase a previously observed invalidation.
        if row['status'] == 'stale':
            return
        code = exit_code(result)
        status, reason = 'failed', 'Проверка не завершилась с exit_code=0.'
        if state == 'completed' and code == 0:
            status, reason = self._snapshot_state(row)
        db.execute('UPDATE acceptance_checks SET status=?,reason=?,checked_at=?,exit_code=? WHERE job_id=?',
                   (status, reason, now(), code, job_id))

    def _snapshot_state(self, row):
        try:
            expected = json.loads(row['snapshot'])
            if row['source'] == 'runtime_validation' and not expected.get('task_outputs'):
                return 'stale', 'Прежняя проверка не подтверждает полный состав outputs; требуется новая приёмка задачи.'
            current = snapshot(self.store.workspace(row['root']), json.loads(row['request'])['artifact_paths'],
                               task_outputs=expected.get('task_outputs', False))
        except ValueError as error:
            return 'stale', str(error)
        if current != json.loads(row['snapshot']):
            return 'stale', 'Содержимое или состав артефактов изменился после начала проверки.'
        return 'passed', 'Команда проверки завершилась успешно; снимок артефактов совпадает.'

    def summary(self, root):
        task = self.store.get(root)
        empty = dict(status='unverified', summary='Структурированная приёмка ещё не проводилась.',
                     criteria=[], job_id=None, flow=None, checked_at=None, artifacts=[], command=None,
                     exit_code=None, source=None, reason='Нет зарегистрированной проверки.')
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM acceptance_checks WHERE root=? ORDER BY rowid DESC LIMIT 1', (root,)).fetchone()
            if row is None:
                return empty
            job = db.execute('SELECT * FROM codex_jobs WHERE id=?', (row['job_id'],)).fetchone()
            status, reason = row['status'], row['reason']
            if job is None or job['root'] != root or job['kind'] != 'command':
                status, reason = 'failed', 'Связанное задание проверки отсутствует или не соответствует задаче.'
            elif status == 'passed':
                if job['state'] != 'completed' or exit_code(job['result']) != 0:
                    status, reason = 'failed', 'Нет успешного результата связанного задания.'
                else:
                    status, reason = self._snapshot_state(row)
                    if status == 'stale':
                        db.execute("UPDATE acceptance_checks SET status='stale',reason=? WHERE job_id=?", (reason, row['job_id']))
            elif status == 'checking' and job['state'] not in ('queued', 'running'):
                status, reason = 'failed', 'Нет завершённой записи приёмки от исполнителя.'
            if status in ('checking', 'passed'):
                if task['stopped'] or task['state'] in ('failed', 'stopped', 'terminated'):
                    status, reason = 'failed', 'Задача остановлена или завершилась с ошибкой.'
                elif task['state'] != 'completed':
                    status, reason = 'checking', 'Ожидается завершение задачи и всех проверок.'
            labels = {'checking': 'Проверка выполняется.', 'passed': 'Приёмка подтверждена.',
                      'failed': 'Приёмка не пройдена.', 'stale': 'Результат проверки устарел.'}
            return dict(status=status, summary=labels[status], criteria=json.loads(row['request'])['criteria'],
                        job_id=row['job_id'], flow=job['flow'] if job else None, checked_at=row['checked_at'],
                        artifacts=[{'path': path, 'sha256': digest} for path, digest in json.loads(row['snapshot'])['files'].items()],
                        command=job['prompt'] if job else None, exit_code=row['exit_code'], source=row['source'], reason=reason)

    def import_verified_receipt(self, root, job_id, artifact_sha256, criteria, source='external_audit'):
        """Administrative Python-only import of an independently verified receipt."""
        if not isinstance(artifact_sha256, dict) or not artifact_sha256:
            raise ValueError('Нужны проверенные SHA-256 файлов.')
        request = validation_request({'artifact_paths': list(artifact_sha256), 'criteria': criteria})
        if not isinstance(source, str) or not source.strip() or len(source) > 200:
            raise ValueError('Укажите источник независимой проверки.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = db.execute('SELECT * FROM tasks WHERE id=?', (root,)).fetchone()
            job = db.execute('SELECT * FROM codex_jobs WHERE id=?', (job_id,)).fetchone()
            if (task is None or task['state'] != 'completed' or task['stopped'] or job is None or
                    job['root'] != root or job['kind'] != 'command' or job['state'] != 'completed' or exit_code(job['result']) != 0):
                raise ValueError('Импорт требует завершённой задачи и успешной команды этой задачи.')
            self.require_reviewer(db, root, job['flow'], task_level=False)
            evidence = snapshot(self.store.workspace(root), request['artifact_paths'])
            if evidence['files'] != artifact_sha256 or evidence['directories']:
                raise ValueError('Проверенные SHA-256 не совпадают с текущими обычными файлами.')
            existing = db.execute('SELECT * FROM acceptance_checks WHERE job_id=?', (job_id,)).fetchone()
            if existing:
                if (json.loads(existing['request']) != request or json.loads(existing['snapshot']) != evidence or existing['source'] != source):
                    raise ValueError('Этот job уже связан с другой проверкой.')
            else:
                db.execute('''INSERT INTO acceptance_checks(job_id,root,request,snapshot,status,checked_at,exit_code,source,reason)
                              VALUES(?,?,?,?,?,?,?,?,?)''',
                           (job_id, root, json.dumps(request), json.dumps(evidence), 'passed', now(), 0, source,
                            'Импортирована независимо проверенная квитанция; SHA-256 совпадают.'))
        return self.summary(root)
