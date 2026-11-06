"""Local task index and activity journal; Dapr owns workflow execution state."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / 'tasks.sqlite'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, prompt TEXT NOT NULL, created TEXT NOT NULL,
                    state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', stopped INTEGER NOT NULL DEFAULT 0,
                    agent_limit INTEGER NOT NULL, llm_limit INTEGER NOT NULL,
                    llm_calls INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS flows (
                    id TEXT PRIMARY KEY, root TEXT NOT NULL, parent TEXT,
                    agent TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'running');
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, root TEXT NOT NULL,
                    flow TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
                    created TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_root ON events(root, id);
                CREATE INDEX IF NOT EXISTS flows_root ON flows(root);
            ''')
            if 'allow_codex' not in {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}:
                db.execute('ALTER TABLE tasks ADD COLUMN allow_codex INTEGER NOT NULL DEFAULT 0')
            for column in ('resumed_from', 'resumed_as'):
                if column not in {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}:
                    db.execute(f'ALTER TABLE tasks ADD COLUMN {column} TEXT')
            # Existing durable runs keep their original policy on upgrade.
            for column, default in (('command_limit',100),('command_reserve',0),('work_window',0)):
                if column not in {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}:
                    db.execute(f'ALTER TABLE tasks ADD COLUMN {column} INTEGER NOT NULL DEFAULT {default}')
            # Only new tasks opt into automatic recovery; historical failures stay inert.
            for column, definition in (
                ('auto_recover','INTEGER NOT NULL DEFAULT 0'),
                ('stop_requested','INTEGER NOT NULL DEFAULT 0'),
                ('stop_generation','INTEGER NOT NULL DEFAULT 0'),
                ('recovery_attempt','INTEGER NOT NULL DEFAULT 0'),
                ('recovery_after','REAL'), ('recovery_wait_since','REAL'),
                ('recovery_status',"TEXT NOT NULL DEFAULT 'idle'"),
                ('recovery_reason',"TEXT NOT NULL DEFAULT ''")):
                if column not in {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}:
                    db.execute(f'ALTER TABLE tasks ADD COLUMN {column} {definition}')
            db.execute('CREATE TABLE IF NOT EXISTS model_failures (token TEXT PRIMARY KEY, root TEXT NOT NULL, flow TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, created TEXT NOT NULL)')
            for column, definition in (('admitted','INTEGER NOT NULL DEFAULT 0'),
                                       ('assignment',"TEXT NOT NULL DEFAULT ''"),
                                       ('execution_state',"TEXT NOT NULL DEFAULT 'queued'")):
                if column not in {row['name'] for row in db.execute('PRAGMA table_info(flows)')}:
                    db.execute(f'ALTER TABLE flows ADD COLUMN {column} {definition}')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, prompt: str, agent_limit: int, llm_limit: int, allow_codex=False,
               command_limit=1000, command_reserve=100, work_window=16):
        if not prompt.strip() or len(prompt) > 30000:
            raise ValueError('Задача должна содержать от 1 до 30000 символов.')
        if not 1 <= agent_limit <= 2000 or not 1 <= llm_limit <= 20000:
            raise ValueError('Недопустимый лимит агентов или вызовов модели.')
        if not 1 <= command_limit <= 20000 or not 0 <= command_reserve < command_limit or not 1 <= work_window <= 64:
            raise ValueError('Недопустимый бюджет команд, резерв проверки или размер окна работы.')
        task_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO tasks(id,prompt,created,state,agent_limit,llm_limit,allow_codex,command_limit,command_reserve,work_window,auto_recover) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (task_id, prompt.strip(), now(), 'preparing', agent_limit, llm_limit, int(bool(allow_codex)),command_limit,command_reserve,work_window,1))
            db.execute('INSERT INTO flows(id,root,parent,agent) VALUES(?,?,?,?)',
                       (task_id, task_id, None, 'coordinator'))
        try:
            self.workspace(task_id).mkdir(parents=True)
        except OSError:
            self.update(task_id, 'failed', error='Не удалось создать папку задачи.')
            raise
        return self.get(task_id)

    def workspace(self, task_id):
        if len(task_id) != 32 or any(c not in '0123456789abcdef' for c in task_id):
            raise ValueError('Некорректный идентификатор задачи.')
        return self.directory / 'workspaces' / task_id

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return dict(row)

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM tasks ORDER BY created DESC LIMIT 500')]

    def active(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks WHERE state IN ('queued','running','suspended') AND stopped=0")]

    def update(self, task_id, state, result='', error=''):
        with self.connect() as db:
            db.execute('UPDATE tasks SET state=?,result=?,error=? WHERE id=? AND stopped=0',
                       (state, result, error, task_id))

    def root_for(self, flow_id):
        with self.connect() as db:
            row = db.execute('SELECT root FROM flows WHERE id=?', (flow_id,)).fetchone()
        if row is None:
            raise RuntimeError('Неизвестный workflow; доступ к файлам запрещён.')
        return row['root']

    def flow_depth(self, flow_id):
        with self.connect() as db:
            depth = 0
            seen = set()
            while flow_id:
                if flow_id in seen:
                    raise RuntimeError('Цикл в дереве агентов.')
                seen.add(flow_id)
                row = db.execute('SELECT parent FROM flows WHERE id=?', (flow_id,)).fetchone()
                if row is None:
                    raise RuntimeError('Неизвестный workflow.')
                flow_id = row['parent']
                if flow_id:
                    depth += 1
            return depth

    def available_agent_slots(self, root, parent, db=None):
        if db is None:
            with self.connect() as connection:
                return self.available_agent_slots(root, parent, connection)
        limit = db.execute('SELECT agent_limit FROM tasks WHERE id=?', (root,)).fetchone()[0]
        count = db.execute('SELECT count(*) FROM flows WHERE root=? AND parent IS NOT NULL', (root,)).fetchone()[0]
        pending = {row[0] for row in db.execute(
            "SELECT f.id FROM flows f WHERE f.root=? AND f.agent='swarm_lead' AND f.state IN ('running','finishing') AND NOT EXISTS (SELECT 1 FROM flows c WHERE c.parent=f.id)", (root,))}
        return limit - count - len(pending) + int(parent in pending)

    def register_flow(self, flow_id, parent, agent):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM flows WHERE id=?', (flow_id,)).fetchone():
                return
            row = db.execute('SELECT root FROM flows WHERE id=?', (parent,)).fetchone()
            if row is None:
                raise RuntimeError('Родительский workflow не найден.')
            root = row['root']
            if agent.startswith('swarm_') and (self.flow_depth(parent) >= 5 or (agent == 'swarm_lead' and self.flow_depth(parent) >= 4)):
                raise RuntimeError('Достигнут предел глубины агентов: 5.')
            task = db.execute('SELECT * FROM tasks WHERE id=?', (root,)).fetchone()
            if task['stopped']:
                raise RuntimeError('Задача остановлена.')
            count = db.execute('SELECT count(*) FROM flows WHERE root=? AND parent IS NOT NULL', (root,)).fetchone()[0]
            needed = 2 if agent == 'swarm_lead' else 1
            if count >= task['agent_limit'] or (agent.startswith('swarm_') and self.available_agent_slots(root, parent, db) < needed):
                raise RuntimeError('Достигнут лимит дочерних агентов.')
            db.execute('INSERT INTO flows(id,root,parent,agent) VALUES(?,?,?,?)', (flow_id, root, parent, agent))

    def ensure_active(self, root):
        if self.get(root)['stopped']:
            raise RuntimeError('Задача остановлена пользователем.')

    def reserve_call(self, root):
        with self.connect() as db:
            result = db.execute('UPDATE tasks SET llm_calls=llm_calls+1 WHERE id=? AND stopped=0 AND llm_calls<llm_limit', (root,))
            if result.rowcount != 1:
                raise RuntimeError('Задача остановлена или исчерпан лимит вызовов модели.')

    def stop(self, root):
        self.get(root)
        stopped = []
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Follow an already-created continuation under the same write lock as resume.
            while root and root not in stopped:
                row = db.execute('SELECT resumed_as FROM tasks WHERE id=?',(root,)).fetchone()
                if row is None:
                    break
                db.execute("UPDATE tasks SET stopped=1,stop_requested=1,stop_generation=stop_generation+1,recovery_status='disabled',recovery_after=NULL,recovery_reason='Остановлено пользователем',state=CASE WHEN resumed_as IS NULL THEN 'stopped' ELSE state END WHERE id=?",(root,))
                stopped.append(root)
                root = row['resumed_as']
        return stopped

    def event(self, root, flow, kind, content):
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        with self.connect() as db:
            db.execute('INSERT INTO events(root,flow,kind,content,created) VALUES(?,?,?,?,?)',
                       (root, flow, kind, content[:30000], now()))

    def events(self, root):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM events WHERE root=? ORDER BY id DESC LIMIT 250', (root,))][::-1]

    def flows(self, root):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM flows WHERE root=?', (root,))]

    def flow_state(self, flow, state):
        with self.connect() as db:
            current = db.execute('SELECT state FROM flows WHERE id=?',(flow,)).fetchone()
            terminal = ('completed','failed','terminated','stopped','stalled')
            if current and current['state'] in terminal and state not in terminal:
                return
            db.execute('UPDATE flows SET state=? WHERE id=?', (state, flow))
            if state in ('completed','failed','terminated','stopped','stalled','finishing'):
                db.execute('UPDATE flows SET admitted=0,execution_state=? WHERE id=?', (state,flow))

    def admit_flow(self, flow):
        """Persistent specialist admission; parents relinquish it before child dispatch."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT f.*,t.work_window,t.stopped FROM flows f JOIN tasks t ON t.id=f.root WHERE f.id=?',(flow,)).fetchone()
            if row is None or row['stopped']:
                raise RuntimeError('Задача остановлена или workflow не найден.')
            if row['state'] in ('completed','failed','terminated','stopped','stalled'):
                raise RuntimeError('Workflow уже завершён.')
            bounded = row['work_window'] and row['agent'] in ('swarm_analyst','swarm_builder','swarm_reviewer','swarm_researcher')
            if bounded and not row['admitted']:
                count = db.execute('SELECT count(*) FROM flows WHERE root=? AND admitted=1',(row['root'],)).fetchone()[0]
                if count >= row['work_window']:
                    db.execute("UPDATE flows SET execution_state='queued' WHERE id=?",(flow,))
                    return False
                db.execute('UPDATE flows SET admitted=1 WHERE id=?',(flow,))
            db.execute("UPDATE flows SET execution_state='model' WHERE id=?",(flow,))
            return True

    def execution_state(self, flow, state):
        with self.connect() as db:
            db.execute('UPDATE flows SET execution_state=? WHERE id=? AND state NOT IN (\'completed\',\'failed\',\'terminated\',\'stopped\')',(state,flow))

    def release_flow(self, flow, state):
        with self.connect() as db:
            db.execute("UPDATE flows SET admitted=0,execution_state=CASE WHEN state IN ('completed','failed','terminated','stopped','stalled') THEN state ELSE ? END WHERE id=?",(state,flow))
