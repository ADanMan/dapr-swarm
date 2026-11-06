"""Standard-library client for the local Swarm HTTP API."""
import argparse
from contextlib import contextmanager
import http.client
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid

JSON_LIMIT = 8 * 1024 * 1024
FILE_LIMIT = 10 * 1024 * 1024
DOWNLOAD_LIMIT = 100 * 1024 * 1024


class CLIError(Exception):
    pass


class DeadlineExceeded(CLIError):
    pass


@contextmanager
def wait_deadline(seconds):
    """macOS/POSIX wall-clock bound, including slowly streamed HTTP headers/body."""
    def expired(*_):
        # Not OSError: urllib must not wrap this in URLError.
        raise DeadlineExceeded('Срок ожидания истёк. Задача продолжает работу.')
    previous_handler = signal.signal(signal.SIGALRM, expired)
    started = time.monotonic()
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL,
                             max(0.000001, previous_timer[0] - (time.monotonic() - started)),
                             previous_timer[1])


def safe_text(value):
    """Keep line breaks; escape control/format characters before terminal output."""
    return ''.join(c if c in '\n\t' or not unicodedata.category(c).startswith('C')
                   else f'\\u{ord(c):04x}' for c in str(value))


def positive(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Нужно положительное число.') from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('Нужно конечное положительное число.')
    return number


def task_id(value):
    if not re.fullmatch('[a-f0-9]{32}', value):
        raise argparse.ArgumentTypeError('ID задачи: 32 строчных шестнадцатеричных символа.')
    return value


def base_url(value):
    match = re.fullmatch(r'http://(127\.0\.0\.1|localhost)(?::([0-9]+))?/?', value)
    if not match or (match[2] is not None and not 1 <= int(match[2]) <= 65535):
        raise argparse.ArgumentTypeError('Допустим только http://127.0.0.1[:порт] или http://localhost[:порт].')
    return value.rstrip('/')


def remote_path(value):
    if not value or value.startswith('/') or '\\' in value or any(unicodedata.category(c).startswith('C') for c in value):
        raise argparse.ArgumentTypeError('Нужен безопасный путь внутри задачи.')
    if any(part in ('', '.', '..') for part in value.split('/')):
        raise argparse.ArgumentTypeError('Путь не может содержать пустые части, . или ...')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, url, timeout):
        self.url, self.timeout = url, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def open(self, route, data=None, content_type=None, timeout=None):
        headers = {'Accept': 'application/json'}
        if content_type:
            headers['Content-Type'] = content_type
        request = urllib.request.Request(self.url + route, data=data, headers=headers)
        try:
            return self.opener.open(request, timeout=self.timeout if timeout is None else timeout)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise CLIError(f'HTTP {code}. Запрос не выполнен; автоматического повтора нет.') from None

    @staticmethod
    def length(response, limit):
        value = response.headers.get('Content-Length')
        if value is None:
            return None
        try:
            size = int(value)
        except ValueError:
            raise CLIError('Сервер вернул некорректный размер ответа.') from None
        if not 0 <= size <= limit:
            raise CLIError('Ответ превышает допустимый размер.')
        return size

    def json(self, route, data=None, content_type=None, timeout=None):
        with self.open(route, data, content_type, timeout) as response:
            expected = self.length(response, JSON_LIMIT)
            raw = response.read(JSON_LIMIT + 1)
        if len(raw) > JSON_LIMIT:
            raise CLIError('JSON-ответ превышает 8 MiB.')
        if expected is not None and len(raw) != expected:
            raise CLIError('Ответ сервера получен не полностью.')
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise CLIError('Сервер вернул некорректный JSON.') from None
        if not isinstance(result, (dict, list)):
            raise CLIError('Неожиданный формат JSON-ответа.')
        return result

    def download(self, route, destination):
        destination = Path(destination).expanduser()
        if os.path.lexists(destination):
            raise CLIError('Файл назначения уже существует; перезапись запрещена.')
        temporary = None
        try:
            with self.open(route) as response:
                expected = self.length(response, DOWNLOAD_LIMIT)
                fd, temporary = tempfile.mkstemp(prefix='.swarm-', dir=destination.parent)
                total = 0
                with os.fdopen(fd, 'wb') as output:
                    while chunk := response.read(min(65536, DOWNLOAD_LIMIT - total + 1)):
                        total += len(chunk)
                        if total > DOWNLOAD_LIMIT:
                            raise CLIError('Скачивание превышает 100 MiB.')
                        output.write(chunk)
                    if expected is not None and total != expected:
                        raise CLIError('Файл получен не полностью.')
                    output.flush()
                    os.fsync(output.fileno())
            # Same directory/filesystem. link() fails atomically if destination exists.
            os.link(temporary, destination)
            return {'path': str(destination), 'size': total}
        finally:
            if temporary is not None:
                os.unlink(temporary)


def multipart(args):
    if (args.text is None) == (args.prompt_file is None):
        raise CLIError('Укажите текст задачи или --prompt-file, ровно один вариант.')
    prompt = args.text
    if args.prompt_file is not None:
        with Path(args.prompt_file).expanduser().open('rb') as file:
            raw = file.read(120001)
        if len(raw) > 120000:
            raise CLIError('Файл задания слишком большой.')
        prompt = raw.decode('utf-8')
    if not prompt.strip() or len(prompt) > 30000:
        raise CLIError('Задание должно содержать 1–30000 символов, кроме одних пробелов.')
    if not 1 <= args.agents <= 2000 or not 1 <= args.calls <= 20000:
        raise CLIError('Лимиты: 1–2000 агентов, 1–20000 обращений.')
    if not 1 <= args.commands <= 20000:
        raise CLIError('Лимит команд: 1–20000.')
    if not 0 <= args.command_reserve < args.commands:
        raise CLIError('Резерв команд должен быть от 0 до лимита команд минус 1.')
    if not 1 <= args.work_window <= 64:
        raise CLIError('Окно работы: 1–64.')
    if len(args.file) > 10:
        raise CLIError('Допускается не больше 10 вложений.')
    uploads, names = [], set()
    for item in args.file:
        path = Path(item).expanduser()
        name = path.name
        if not name or name in ('.', '..') or any(c in name for c in '\\"\r\n') or any(unicodedata.category(c).startswith('C') for c in name):
            raise CLIError('Некорректное имя вложения.')
        if name in names:
            raise CLIError('Имена вложений должны быть уникальны.')
        names.add(name)
        if not path.is_file() or path.stat().st_size > FILE_LIMIT:
            raise CLIError('Вложение должно быть обычным файлом размером до 10 MiB.')
        with path.open('rb') as file:
            content = file.read(FILE_LIMIT + 1)
        if len(content) > FILE_LIMIT:
            raise CLIError('Вложение превышает 10 MiB.')
        uploads.append((name, content))
    boundary = 'swarm-' + uuid.uuid4().hex
    parts = []
    fields = {'prompt': prompt, 'agent_limit': str(args.agents), 'llm_limit': str(args.calls),
              'command_limit': str(args.commands), 'command_reserve': str(args.command_reserve),
              'work_window': str(args.work_window),
              'allow_codex': 'true' if args.allow_codex else 'false'}
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, content in uploads:
        parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(), content, b'\r\n'])
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'


def parser():
    root = argparse.ArgumentParser(prog='swarm', description='Рой: задачи, результаты и файлы через локальный API.')
    root.add_argument('--base-url', type=base_url, default='http://127.0.0.1:8788')
    root.add_argument('--json', action='store_true', help='Один JSON-документ в stdout')
    root.add_argument('--timeout', type=positive, default=30, help='Таймаут HTTP, секунды')
    commands = root.add_subparsers(dest='command', required=True)
    commands.add_parser('health', help='Состояние сервиса')
    commands.add_parser('list', help='Список задач')
    submit = commands.add_parser('submit', help='Создать задачу')
    submit.add_argument('text', nargs='?')
    submit.add_argument('--prompt-file')
    submit.add_argument('--file', action='append', default=[])
    submit.add_argument('--agents', type=int, default=2000)
    submit.add_argument('--calls', type=int, default=20000)
    submit.add_argument('--commands', type=int, default=1000, help='Лимит команд: 1–20000 (по умолчанию 1000)')
    submit.add_argument('--command-reserve', type=int, default=100,
                        help='Резерв команд на проверку: от 0 до лимита минус 1 (по умолчанию 100)')
    submit.add_argument('--work-window', type=int, default=16, help='Окно работы: 1–64 (по умолчанию 16)')
    submit.add_argument('--allow-codex', action='store_true', help='Устаревший параметр; новые задачи принимают только GLM')
    for name, description in [('show', 'Задача и результат'), ('events', 'Последние доступные события (до 250)'),
                              ('files', 'Файлы задачи'), ('stop', 'Остановить задачу'),
                              ('resume', 'Продолжить остановленную задачу с файлами и контекстом'),
                              ('wait', 'Дождаться завершения'), ('download', 'Скачать файл без перезаписи')]:
        command = commands.add_parser(name, help=description)
        command.add_argument('id', type=task_id)
        if name == 'wait':
            command.add_argument('--interval', type=positive, default=2)
            command.add_argument('--deadline', type=positive, default=600)
        elif name == 'download':
            command.add_argument('path', type=remote_path)
            command.add_argument('--output', required=True)
    return root


def human(command, data):
    if command == 'health':
        return f"Сервис: {'готов' if data.get('ready') else 'не готов'}\nМодель: {data.get('model', '')}\n{data.get('error', '')}".rstrip()
    if command == 'list':
        return '\n'.join(f"{t['id']}  {t['state']}  {t.get('prompt', '').splitlines()[0][:100]}" for t in data) or 'Задач пока нет.'
    if command == 'events':
        return '\n'.join(f"{e.get('created', '')}  {e.get('kind', '')}: {e.get('content', '')}" for e in data) or 'Событий пока нет.'
    if command == 'files':
        return '\n'.join(f"{f['path']}  {f['size']} байт" for f in data) or 'Файлов пока нет.'
    if command == 'download':
        return f"Сохранено: {data['path']} ({data['size']} байт)"
    budget = data.get('command_budget')
    budget_text = ''
    if isinstance(budget, dict):
        budget_text = (f"Команды: {budget.get('used', '?')}/{budget.get('limit', '?')}; "
                       f"осталось: {budget.get('remaining', '?')}; "
                       f"работа: {budget.get('work_remaining', '?')}; "
                       f"проверка: {budget.get('review_remaining', '?')}; "
                       f"резерв: {budget.get('reserve', '?')}")
    return '\n'.join(str(value) for value in [f"{data['id']}  {data['state']}", data.get('prompt', ''),
                                              budget_text,
                                              data.get('result', ''), data.get('error', ''),
                                              ('Продолжение задачи: ' + data['resumed_from']) if data.get('resumed_from') else ''] if value)


def main(argv=None):
    try:
        # argparse signals help/invalid arguments with SystemExit. Public main returns a code.
        args = parser().parse_args(argv)
        client = Client(args.base_url, args.timeout)
        route = '/api/tasks/' + args.id if hasattr(args, 'id') else '/api/tasks'
        code = 0
        if args.command == 'health':
            data = client.json('/api/health')
        elif args.command == 'list':
            data = client.json(route)
        elif args.command == 'submit':
            body, content_type = multipart(args)
            data = client.json(route, body, content_type)
        elif args.command in ('stop', 'resume'):
            data = client.json(route + '/' + args.command, b'')
        elif args.command == 'download':
            data = client.download(route + '/files/' + urllib.parse.quote(args.path, safe='/'), args.output)
        elif args.command == 'wait':
            with wait_deadline(args.deadline):
                deadline = time.monotonic() + args.deadline
                seen = {args.id}
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('Срок ожидания истёк. Задача продолжает работу.')
                    data = client.json(route, timeout=min(client.timeout, remaining))
                    state = data['state']
                    recovery = data.get('recovery') or {}
                    if recovery.get('enabled') and recovery.get('status') == 'continued':
                        next_id = recovery.get('next_task_id')
                        if not isinstance(next_id,str) or not re.fullmatch('[a-f0-9]{32}',next_id) or next_id in seen:
                            raise CLIError('Некорректная цепочка автоматического продолжения.')
                        seen.add(next_id)
                        route = '/api/tasks/' + next_id
                        continue
                    pending = recovery.get('enabled') and recovery.get('status') == 'scheduled'
                    if state in ('completed', 'failed', 'stopped') and not pending:
                        code = 0 if state == 'completed' else 1
                        break
                    time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
        else:
            data = client.json(route)
            if args.command in ('events', 'files'):
                data = data[args.command]
        print(json.dumps(data, ensure_ascii=False) if args.json else safe_text(human(args.command, data)))
        return code
    except SystemExit as error:
        return int(error.code)
    except KeyboardInterrupt:
        print('Ожидание прервано. Задача не остановлена.', file=sys.stderr)
        return 130
    except (DeadlineExceeded, TimeoutError) as error:
        print(safe_text(error), file=sys.stderr)
        return 3
    except (CLIError, OSError, urllib.error.URLError, http.client.HTTPException, ValueError, KeyError, TypeError) as error:
        print('Ошибка: ' + safe_text(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
