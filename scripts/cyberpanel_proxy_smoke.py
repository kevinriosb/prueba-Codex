"""Prueba local con OpenLiteSpeed real y backend sin IA; no accede al VPS.

Requiere las imágenes locales prueba-codex-agent:local y
litespeedtech/openlitespeed:latest, Docker y openssl. Certificado y datos ficticios.
"""
import http.client
import http.cookies
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import time
import uuid

from configure_cyberpanel_proxy import render


IMAGE = 'litespeedtech/openlitespeed:latest'
DOMAIN = 'agente.example.test'


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def run():
    containers = []
    with tempfile.TemporaryDirectory(prefix='cyberpanel-test-') as directory:
        root = Path(directory)
        root.chmod(0o755)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=' + DOMAIN, '-addext', 'subjectAltName=DNS:' + DOMAIN + ',IP:127.0.0.1',
                        '-keyout', str(root / 'server.key'), '-out', str(root / 'server.crt')],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        certificate = ssl.create_default_context(cafile=str(root / 'server.crt'))
        global_config = docker('run', '--rm', '--entrypoint', 'cat', IMAGE,
                               '/usr/local/lsws/conf/httpd_config.conf')
        # Omitir los sitios de demostración Docker adicionales; solo probar Example.
        global_config = global_config.split('\nlistener HTTP {', 1)[0]
        global_config += '''
listener AgentTestTLS {
  address *:8443
  secure 1
  keyFile /tmp/agent-test/server.key
  certFile /tmp/agent-test/server.crt
  map Example agente.example.test
}
'''
        (root / 'server.conf').write_text(global_config)
        original = '''docRoot $VH_ROOT/html
vhDomain $VH_NAME
accesslog $VH_ROOT/logs/agent-test.log {
  useServer 0
  logFormat "%h %r %>s"
  logHeaders 5
  rollingSize 10M
}
rewrite {
  enable 1
  autoLoadHtaccess 1
}
context /.well-known/acme-challenge {
  location /usr/local/lsws/Example/html/.well-known/acme-challenge
  allowBrowse 1
  rewrite {
    enable 0
  }
}
vhssl {
  keyFile /tmp/agent-test/server.key
  certFile /tmp/agent-test/server.crt
  certChain 1
}
'''
        (root / 'vhost.conf').write_text(render(original, DOMAIN))
        (root / 'acme').mkdir(mode=0o755)
        (root / 'acme').chmod(0o755)
        (root / 'acme' / 'challenge-test').write_text('acme-conservado')
        (root / 'acme' / 'challenge-test').chmod(0o644)
        try:
            server = docker('run', '--rm', '-d', '-p', '127.0.0.1::8088', '-p', '127.0.0.1::8443',
                            '-v', str(root) + ':/tmp/agent-test:ro',
                            '-v', str(root / 'server.conf') + ':/usr/local/lsws/conf/httpd_config.conf:ro',
                            '-v', str(root / 'vhost.conf') + ':/usr/local/lsws/conf/vhosts/Example/vhconf.conf:ro',
                            '-v', str(root / 'acme') + ':/usr/local/lsws/Example/html/.well-known/acme-challenge:ro',
                            '--entrypoint', '/usr/local/lsws/bin/lshttpd', IMAGE, '-n')
            containers.append(server)
            # CyberPanel crea el docroot con un usuario del sitio, no con root.
            docker('exec', server, 'chown', '1000:1000', '/usr/local/lsws/Example/html')
            backend = docker('run', '--rm', '-d', '--network', 'container:' + server,
                             '-e', 'HOST=127.0.0.1', '-e', 'PORT=18000',
                             '-e', 'APP_LOGIN_PASSWORD=solo-prueba-proxy-123456',
                             '-e', 'APP_ALLOWED_HOSTS=' + DOMAIN + ',localhost,127.0.0.1',
                             '-e', 'AI_API_KEY=', 'prueba-codex-agent:local')
            containers.append(backend)
            ports = json.loads(docker('inspect', '--format', '{{json .NetworkSettings.Ports}}', server))

            def request(path, *, secure=True, method='GET', data=None, cookie=None, csrf=None, origin=None):
                port = int(ports['8443/tcp' if secure else '8088/tcp'][0]['HostPort'])
                connection = http.client.HTTPSConnection('127.0.0.1', port, context=certificate, timeout=5) if secure else http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                headers = {'Host': DOMAIN, 'Origin': origin or 'https://' + DOMAIN}
                if cookie:
                    headers['Cookie'] = cookie
                if csrf:
                    headers['X-CSRF-Token'] = csrf
                payload = json.dumps(data).encode() if data is not None else None
                if payload:
                    headers['Content-Type'] = 'application/json'
                try:
                    connection.request(method, path, body=payload, headers=headers)
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), response.read(20000)
                finally:
                    connection.close()

            for attempt in range(80):
                try:
                    status, headers, body = request('/health')
                    if status == 200 and json.loads(body).get('status') == 'ok':
                        break
                except (OSError, ValueError, http.client.HTTPException):
                    pass
                if attempt == 79:
                    raise RuntimeError('El proxy no respondió a /health.')
                time.sleep(.25)
            validation = subprocess.run(['docker', 'exec', server, '/usr/local/lsws/bin/lshttpd', '-t'], capture_output=True, text=True)
            assert validation.returncode == 0, validation.stdout + validation.stderr
            assert '[ERROR]' not in validation.stdout + validation.stderr
            assert headers.get('cache-control', headers.get('Cache-Control')) == 'no-store'
            status, headers, _ = request('/health', secure=False)
            assert status in {301, 302} and headers.get('location', headers.get('Location')) == 'https://' + DOMAIN + '/health'
            acme = request('/.well-known/acme-challenge/challenge-test', secure=False)
            assert acme[0] == 200 and acme[2] == b'acme-conservado', acme
            assert request('/api/config')[0] == 401
            status, headers, body = request('/api/auth/login', method='POST', data={'username': 'admin', 'password': 'solo-prueba-proxy-123456'})
            assert status == 200, (status, body)
            cookies = http.cookies.SimpleCookie()
            cookies.load(headers.get('set-cookie', headers.get('Set-Cookie')))
            assert all(cookie['secure'] and cookie['httponly'] for cookie in cookies.values())
            cookie = '; '.join(item.key + '=' + item.value for item in cookies.values())
            assert json.loads(request('/api/config', cookie=cookie)[2])['ai_configured'] is False
            assert request('/api/auth/login', method='POST', origin='http://' + DOMAIN, data={'username': 'admin', 'password': 'solo-prueba-proxy-123456'})[0] == 403
            marker = '/log-check-' + uuid.uuid4().hex
            request(marker + '?code=secreto-ficticio-no-registrar')
            docker('exec', server, '/usr/local/lsws/bin/lswsctrl', 'reload')
            for _ in range(30):
                log = docker('exec', server, 'cat', '/usr/local/lsws/Example/logs/agent-test.log')
                if marker in log:
                    break
                time.sleep(.2)
            assert marker in log and 'secreto-ficticio-no-registrar' not in log
            print('PASS: OpenLiteSpeed real, TLS verificado con CA ficticia, Host/Origin, login y cookies seguras, '
                  'HTTP redirigido a HTTPS, ACME preservado, no-store y query strings privadas ausentes del accesslog. '
                  'Sin llamadas de IA, correo o cambios en el VPS.')
        except BaseException:
            if containers:
                # Solo datos ficticios; ayuda a diagnosticar esta prueba local.
                log = docker('exec', containers[0], 'cat', '/usr/local/lsws/logs/error.log')
                print('\n'.join(line for line in log.splitlines() if '[ERROR]' in line or 'acme' in line.lower())[-3000:])
            raise
        finally:
            for identifier in reversed(containers):
                subprocess.run(['docker', 'rm', '-f', identifier], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    run()
