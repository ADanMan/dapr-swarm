"""Start the local stack from explicit environment or ignored .env settings."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit

from dotenv import dotenv_values


def load_settings(root, environ):
    """Read only this checkout's settings; never inspect another application's keys."""
    settings = {**dotenv_values(root / '.env'), **environ}
    names = ('BEEGATE_API_KEY', 'BEEGATE_BASE_URL', 'BEEGATE_MODEL')
    missing = [name for name in names if not (settings.get(name) or '').strip()]
    if missing:
        raise ValueError('Set required variables: ' + ', '.join(missing))
    endpoint = urlsplit(settings['BEEGATE_BASE_URL'])
    if (endpoint.scheme not in ('http', 'https') or not endpoint.hostname
            or endpoint.hostname.endswith('.invalid') or endpoint.username or endpoint.password):
        raise ValueError('BEEGATE_BASE_URL must be a real HTTP(S) API endpoint without embedded credentials.')
    if settings['BEEGATE_MODEL'] == 'your-glm-model-id':
        raise ValueError('Replace BEEGATE_MODEL with the exact model ID available to your account.')
    return {**environ, **{name: settings[name] for name in names},
            'SEARXNG_SECRET': settings.get('SEARXNG_SECRET') or secrets.token_hex(32),
            'LOCAL_UID': str(os.getuid()), 'LOCAL_GID': str(os.getgid())}


def main():
    root = Path(__file__).resolve().parent.parent
    try:
        environment = load_settings(root, os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    key = environment['BEEGATE_API_KEY']
    (root / 'data').mkdir(exist_ok=True, mode=0o700)
    # The worker uses the existing local Codex login and the app-owned queue over loopback HTTP.
    # SQLite is never opened by the host worker; no credentials or host control socket enter the app container.
    with (root / 'data' / 'codex-worker.log').open('a') as worker_log:
        subprocess.Popen([sys.executable, '-m', 'app.codex_worker'], cwd=root,
                         stdin=subprocess.DEVNULL, stdout=worker_log, stderr=worker_log,
                         env={k: v for k, v in os.environ.items() if k in ('HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'TMPDIR')},
                         start_new_session=True)
    subprocess.run(['docker', 'compose', 'up', '-d', '--build'], cwd=root, env=environment, check=True)
    for _ in range(90):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8788/api/health', timeout=2) as response:
                health = json.load(response)
            if health['ready']:
                print('Готово: http://127.0.0.1:8788')
                if '--no-open' not in sys.argv:
                    subprocess.run(['open', 'http://127.0.0.1:8788'], check=False)
                break
            if health['error']:
                raise SystemExit('Dapr не запустился: ' + health['error'].replace(key, '[REDACTED]'))
        except (OSError, ValueError):
            pass
        time.sleep(2)
    else:
        raise SystemExit('Запуск занял больше 3 минут. Проверьте docker compose logs app sidecar.')


if __name__ == "__main__":
    main()
