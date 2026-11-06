"""Loopback-only host worker transport. SQLite is owned exclusively by the app."""
import json
from pathlib import Path
import urllib.error
import urllib.request


class WorkerUnavailable(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RemoteStore:
    def __init__(self, directory, base_url='http://127.0.0.1:8788'):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, route, data=None):
        request = urllib.request.Request(self.base_url + route,
            data=None if data is None else json.dumps(data).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=5) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise WorkerUnavailable('Ответ локального сервиса слишком большой.')
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code >= 500:
                raise WorkerUnavailable(f'Локальный сервис: HTTP {error.code}.') from None
            raise RuntimeError(f'Локальный сервис: HTTP {error.code}.') from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise WorkerUnavailable('Локальный сервис недоступен.') from error

    def workspace(self, task_id):
        if len(task_id) != 32 or any(c not in '0123456789abcdef' for c in task_id):
            raise ValueError('Некорректный идентификатор задачи.')
        return self.directory / 'workspaces' / task_id

    def get(self, root):
        self.workspace(root)
        return self.request('/api/worker/tasks/' + root)

    def ensure_active(self, root):
        if self.get(root)['stopped']:
            raise RuntimeError('Задача остановлена пользователем.')

    def event(self, root, flow, kind, content):
        self.request('/api/worker/events', {'root': root, 'flow': flow, 'kind': kind, 'content': str(content)[:30000]})


class RemoteJobs:
    def __init__(self, store):
        self.store = store

    def claim(self):
        return self.store.request('/api/worker/claim', {})

    def interrupted(self):
        return self.store.request('/api/worker/running')

    def set_pid(self, job_id, pid):
        self.store.request('/api/worker/jobs/' + job_id + '/pid', {'pid': pid})

    def finish(self, job_id, state, result='', error=''):
        self.store.request('/api/worker/jobs/' + job_id + '/finish',
                           {'state': state, 'result': result[:24000], 'error': error[:1000]})
