"""Prueba Docker local, sin IA ni servicios de pago; limpia sus propios recursos."""
import http.cookiejar
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def docker(*arguments):
    return subprocess.check_output(['docker', *arguments], text=True).strip()


def run():
    volume = 'personal-agent-test-' + uuid.uuid4().hex[:12]
    containers = []
    docker('volume', 'create', volume)
    try:
        def start():
            identifier = docker('run', '--rm', '-d', '-p', '127.0.0.1::8000',
                '-v', volume + ':/data', '-e', 'APP_ENV=development', '-e', 'APP_SECURE_COOKIES=false',
                '-e', 'APP_LOGIN_PASSWORD=solo-para-pruebas-de-contenedor', '-e', 'APP_ALLOWED_HOSTS=localhost,127.0.0.1',
                'prueba-codex-agent:local')
            containers.append(identifier)
            port = docker('port', identifier, '8000').split(':')[-1]
            base = 'http://127.0.0.1:' + port
            for _ in range(100):
                try:
                    with urllib.request.urlopen(base + '/health', timeout=1) as response:
                        assert json.load(response)['status'] == 'ok'
                        return identifier, base
                except (OSError, TimeoutError):
                    time.sleep(.1)
            raise RuntimeError('El contenedor no respondió a /health')

        first, base = start()
        assert docker('exec', first, 'id', '-u') == '10001'
        assert docker('exec', first, 'python', '-c', "from pathlib import Path; assert not Path('/app/.env').exists(); assert not Path('/app/tests').exists(); print('ok')") == 'ok'
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def request(base, path, method='GET', data=None, csrf=None):
            headers = {'Origin': base, 'Content-Type': 'application/json'}
            if csrf:
                headers['X-CSRF-Token'] = csrf
            query = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None, headers=headers, method=method)
            with opener.open(query, timeout=5) as response:
                return json.load(response)

        auth = request(base, '/api/auth/login', 'POST', {'username': 'admin', 'password': 'solo-para-pruebas-de-contenedor'})
        conversation = request(base, '/api/conversations', 'POST', csrf=auth['csrf_token'])
        assert request(base, '/api/config')['ai_configured'] is False
        docker('stop', first)
        containers.remove(first)
        second, base = start()
        auth = request(base, '/api/auth/login', 'POST', {'username': 'admin', 'password': 'solo-para-pruebas-de-contenedor'})
        assert request(base, '/api/conversations/' + conversation['id'])['id'] == conversation['id']
        print('PASS: imagen local, /health, login, usuario sin privilegios, historial conservado al recrear el contenedor con volumen y exclusión de .env y pruebas.')
    finally:
        for identifier in containers:
            subprocess.run(['docker', 'rm', '-f', identifier], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'volume', 'rm', volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    run()
