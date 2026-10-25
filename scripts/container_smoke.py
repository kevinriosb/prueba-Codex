"""Prueba Docker local, sin IA ni servicios de pago; limpia sus propios recursos."""
import http.cookiejar
import base64
import io
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from PIL import Image


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
                '-e','AI_API_KEY=','-e','BILLING_ENCRYPTION_KEY='+base64.urlsafe_b64encode(b't'*32).decode(),
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

        def request(base, path, method='GET', data=None, csrf=None,raw=False):
            headers = {'Origin': base, 'Content-Type': 'application/octet-stream' if isinstance(data,bytes) else 'application/json'}
            if csrf:
                headers['X-CSRF-Token'] = csrf
            payload=data if isinstance(data,bytes) else json.dumps(data).encode() if data is not None else None
            query = urllib.request.Request(base + path, data=payload, headers=headers, method=method)
            with opener.open(query, timeout=5) as response:
                return response.read() if raw else json.load(response)

        auth = request(base, '/api/auth/login', 'POST', {'username': 'admin', 'password': 'solo-para-pruebas-de-contenedor'})
        conversation = request(base, '/api/conversations', 'POST', csrf=auth['csrf_token'])
        assert request(base, '/api/config')['ai_configured'] is False
        assert request(base,'/api/billing/config')['enabled'] is True
        image=io.BytesIO()
        Image.new('RGB',(16,16),'white').save(image,format='PNG')
        document=request(base,'/api/billing/documents?purpose=csf','POST',image.getvalue(),auth['csrf_token'])
        before=request(base,'/api/billing/documents/'+document['id'],raw=True)
        assert before.startswith(b'\xff\xd8')
        docker('stop', first)
        containers.remove(first)
        second, base = start()
        auth = request(base, '/api/auth/login', 'POST', {'username': 'admin', 'password': 'solo-para-pruebas-de-contenedor'})
        assert request(base, '/api/conversations/' + conversation['id'])['id'] == conversation['id']
        assert request(base,'/api/billing/documents/'+document['id'],raw=True)==before
        print('PASS: imagen local, /health, login, usuario sin privilegios, historial y documento fiscal cifrado recuperados al recrear el contenedor con volumen, exclusión de .env/pruebas y ninguna llamada de IA.')
    finally:
        for identifier in containers:
            subprocess.run(['docker', 'rm', '-f', identifier], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'volume', 'rm', volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    run()
