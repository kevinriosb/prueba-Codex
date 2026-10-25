"""Prueba Compose local con secretos ficticios; no accede al VPS ni a APIs.

Usa una imagen ya construida llamada prueba-codex-agent:local. Requiere Compose
2.30+ y Python 3.9+. Elimina exclusivamente su proyecto y volumen temporales.
"""
import base64
import http.cookies
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid

from configure_hostinger import build_configuration, write_new_configuration


ROOT = Path(__file__).resolve().parents[1]


def run():
    password = 'prueba$LILYAPP_TEST_RAW#\'"=contraseña'
    domain = 'agente.example.test'
    project = 'lilyapp-test-' + uuid.uuid4().hex[:12]
    child_env = dict(os.environ, LILYAPP_TEST_RAW='NO_SUSTITUIR')
    with tempfile.TemporaryDirectory(prefix='lilyapp-compose-') as directory:
        temporary = Path(directory)
        private = temporary / '.env.hostinger'
        template = (ROOT / '.env.hostinger.example').read_text(encoding='utf-8')
        content = build_configuration(domain, 'admin', password, '', template)
        write_new_configuration(private, content)
        assert private.stat().st_mode & 0o777 == 0o600
        try:
            write_new_configuration(private, 'no debe sustituir la clave')
        except FileExistsError:
            pass
        else:
            raise AssertionError('La configuración existente se sobrescribió')
        assert private.read_text(encoding='utf-8') == content
        override = temporary / 'test.yaml'
        override.write_text('''services:
  agent:
    image: prueba-codex-agent:local
    pull_policy: never
    ports: !override
      - "127.0.0.1::8000"
    healthcheck:
      interval: 1s
      start_period: 1s
''', encoding='utf-8')
        command = ['docker', 'compose', '--project-directory', directory, '-p', project,
                   '-f', str(ROOT / 'compose.hostinger.yaml'), '-f', str(override)]

        def compose(*arguments):
            return subprocess.check_output(command + list(arguments), env=child_env, text=True).strip()

        configuration = json.loads(compose('config', '--format', 'json'))
        service = configuration['services']['agent']
        # `compose config` escapa $ para serializar su resultado. El login real
        # comprueba abajo que el contenedor recibe la contraseña literal.
        assert service['environment']['AI_API_KEY'] == ''
        assert service['environment']['APP_ENV'] == 'production'
        assert all(port['host_ip'] == '127.0.0.1' for port in service['ports'])
        assert service['read_only'] is True

        def start():
            compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '60')
            identifier = compose('ps', '-q', 'agent')
            binding = json.loads(subprocess.check_output(
                ['docker', 'inspect', '--format', '{{json .NetworkSettings.Ports}}', identifier],
                text=True))['8000/tcp']
            assert len(binding) == 1 and binding[0]['HostIp'] == '127.0.0.1'
            assert compose('exec', '-T', 'agent', 'id', '-u') == '10001'
            compose('exec', '-T', 'agent', 'python', '-c',
                    "import os; from pathlib import Path; assert os.statvfs('/').f_flag & os.ST_RDONLY; "
                    "assert not Path('/app/.env.hostinger').exists(); "
                    "assert not Path('/app/scripts').exists()")
            return 'http://127.0.0.1:' + binding[0]['HostPort']

        def request(base, path, *, data=None, csrf=None, cookie=None, host=domain, raw=False):
            headers = {'Host': host, 'Origin': 'https://' + domain}
            if csrf:
                headers['X-CSRF-Token'] = csrf
            if cookie:
                headers['Cookie'] = cookie
            if data is not None:
                headers['Content-Type'] = 'application/octet-stream' if isinstance(data, bytes) else 'application/json'
            payload = data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else None
            with urllib.request.urlopen(urllib.request.Request(base + path, data=payload, headers=headers), timeout=10) as response:
                body = response.read()
                return body if raw else json.loads(body), response.headers

        def login(base):
            result, headers = request(base, '/api/auth/login', data={'username': 'admin', 'password': password})
            cookies = http.cookies.SimpleCookie()
            cookies.load(headers['Set-Cookie'])
            assert all(cookie['secure'] and cookie['httponly'] and cookie['samesite'] == 'strict' for cookie in cookies.values())
            return result['csrf_token'], '; '.join(cookie.key + '=' + cookie.value for cookie in cookies.values())

        try:
            base = start()
            assert request(base, '/health')[0]['status'] == 'ok'
            try:
                request(base, '/health', host='incorrecto.example.test')
            except urllib.error.HTTPError as error:
                assert error.code == 400
            else:
                raise AssertionError('Se aceptó un host no autorizado')
            csrf, cookie = login(base)
            assert request(base, '/api/config', cookie=cookie)[0]['ai_configured'] is False
            assert request(base, '/api/billing/config', cookie=cookie)[0]['enabled'] is True
            conversation = request(base, '/api/conversations', data={}, csrf=csrf, cookie=cookie)[0]
            corrupted = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC')
            try:
                request(base, '/api/billing/documents?purpose=csf', data=corrupted, csrf=csrf, cookie=cookie)
            except urllib.error.HTTPError as error:
                assert error.code == 422
            else:
                raise AssertionError('Se aceptó una imagen corrupta')
            # PNG de un píxel, generado para esta prueba; no es una CSF real.
            image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC')
            document = request(base, '/api/billing/documents?purpose=csf', data=image, csrf=csrf, cookie=cookie)[0]
            original = request(base, '/api/billing/documents/' + document['id'], cookie=cookie, raw=True)[0]
            assert original.startswith(b'\xff\xd8')
            compose('down', '--timeout', '45')
            base = start()
            csrf, cookie = login(base)
            assert request(base, '/api/conversations/' + conversation['id'], cookie=cookie)[0]['id'] == conversation['id']
            assert request(base, '/api/billing/documents/' + document['id'], cookie=cookie, raw=True)[0] == original
            print('PASS: Compose de producción, puerto local, cookies HTTPS, contraseña literal, '
                  'configuración privada no sobrescrita, usuario sin privilegios, raíz de solo lectura y '
                  'persistencia del chat/documento cifrado al recrear el contenedor. Sin llamadas a APIs.')
        finally:
            compose('down', '--volumes', '--timeout', '45')


if __name__ == '__main__':
    run()
