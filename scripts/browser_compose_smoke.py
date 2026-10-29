"""Prueba la conexión agente/navegador con proyecto y credenciales ficticios."""
import json
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path


PROBE="""
import json,urllib.request,urllib.error
from pathlib import Path
token=Path('/run/browser-auth/token').read_text().strip()
request=urllib.request.Request('http://browser:8001/v1/sessions',
 data=json.dumps({'owner':'test-owner','url':'file:///etc/passwd','allowed':['https://fixture.example']}).encode(),
 headers={'Content-Type':'application/json','X-Browser-Key':token})
try: urllib.request.urlopen(request,timeout=5)
except urllib.error.HTTPError as error: assert error.code==422
else: raise AssertionError('El navegador no rechazó la URL local.')
print('PASS: credencial interna compartida, red privada y rechazo de URL local desde el agente.')
"""


def main():
    root=Path(__file__).resolve().parents[1]
    project='browser-compose-test-'+uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix='browser-compose-') as directory:
        temporary=Path(directory)
        (temporary/'.env.hostinger').write_text('APP_LOGIN_PASSWORD=fake-compose-test-password\nAI_API_KEY=\n'
            'BILLING_ENCRYPTION_KEY=dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHQ=\n'
            '')
        override=temporary/'override.yaml'
        override.write_text('''services:
  agent:
    image: prueba-codex-agent:local
    ports: !override ["127.0.0.1::8000"]
  browser:
    image: prueba-codex-browser:local
  browser-token:
    image: prueba-codex-agent:local
''')
        command=['docker','compose','--project-directory',directory,'-p',project,
            '-f',str(root/'compose.hostinger.yaml'),'-f',str(root/'compose.browser.yaml'),'-f',str(override)]
        def compose(*args):
            return subprocess.check_output(command+list(args),text=True).strip()
        try:
            compose('up','-d','--no-build','--wait','--wait-timeout','120')
            port=compose('port','agent','8000').split(':')[-1]
            with urllib.request.urlopen('http://127.0.0.1:'+port+'/health',timeout=5) as response:
                assert json.load(response)['status']=='ok'
            result=subprocess.run(command+['exec','-T','agent','python','-'],input=PROBE,text=True,capture_output=True)
            if result.returncode:
                raise RuntimeError('Falló la comunicación privada de prueba; no se muestran credenciales.')
            print(result.stdout.strip())
            original_browser=compose('ps','-q','browser')
            compose('up','-d','--no-build','--no-deps','--force-recreate','--wait','--wait-timeout','120','agent')
            assert compose('ps','-q','browser')==original_browser
            repeated=subprocess.run(command+['exec','-T','agent','python','-'],input=PROBE,text=True,capture_output=True)
            if repeated.returncode:
                raise RuntimeError('Falló la comunicación después de recrear solo el agente.')
            print('PASS: agente recreado conservando el mismo navegador y la comunicación privada.')
            diagnostic=(root/'scripts/check_browser.py').read_text()
            internal=subprocess.run(command+['exec','-T','agent','python','-','--component','connection'],
                input=diagnostic,text=True,capture_output=True)
            if internal.returncode or 'Autenticación interna: correcta.' not in internal.stdout:
                raise RuntimeError('El diagnóstico por stdin no confirmó la comunicación; salida privada omitida.')
            chromium=subprocess.run(command+['exec','-T','browser','python','-','--component','portal',
                '--url','file:///diagnostic-not-opened'],input=diagnostic,text=True,capture_output=True)
            if chromium.returncode!=1 or 'local correctos' not in chromium.stdout or 'dirección no permitida' not in chromium.stdout:
                raise RuntimeError('El diagnóstico por stdin de Chromium no pasó; no se muestran detalles internos.')
            print('PASS: diagnóstico por stdin en ambas imágenes existentes, sin navegar a comercios ni llamar a IA.')
        finally:
            compose('down','--volumes','--remove-orphans')  # Solo el proyecto aleatorio de prueba.
    print('PASS: stack Compose saludable y recursos ficticios eliminados; ninguna llamada de IA.')


if __name__=='__main__':
    main()
