"""Comprueba el navegador Docker con páginas ficticias y sin llamadas externas."""
import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def docker(*args):
    return subprocess.check_output(['docker',*args],text=True).strip()


CHECK=r'''
import asyncio,os
from pathlib import Path
from backend.browser_worker.engine import BrowserManager
from backend.browser_worker.policy import BrowserError
assert os.getuid()==10001
assert not any(os.environ.get(key) for key in ('AI_API_KEY','OPENAI_API_KEY','BILLING_ENCRYPTION_KEY','APP_LOGIN_PASSWORD'))
assert not Path('/app/.env').exists() and not Path('/data/agent.sqlite3').exists()
class Pages:
 async def fetch(self,url,headers):
  content='<a href="https://provider.example/form">Facturación</a>' if url.endswith('/') else '<label>Cuenta<input></label>'
  return 200,{'content-type':'text/html; charset=utf-8'},content.encode()
 async def close(self):pass
async def run():
 manager=BrowserManager(fetcher=Pages())
 try:
  first=await manager.open('test-owner','https://fixture.example/',['https://fixture.example'])
  assert first['actions'][0]['permission_origin']=='https://provider.example'
  try:await manager.act('test-owner',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
  except BrowserError as error:assert error.code=='browser_origin'
  else:raise AssertionError('Navegó sin consentimiento del nuevo dominio.')
  first=await manager.allow('test-owner',first['session_id'],['https://fixture.example','https://provider.example'])
  second=await manager.act('test-owner',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
  assert second['fields'][0]['label']=='Cuenta'
  assert not second['submitted'] and not second['purchase_validated']
 finally:await manager.close()
asyncio.run(run())
print('PASS: Chromium sin privilegios, páginas simuladas y ningún dato fiscal o llamada de IA.')
'''


def main():
    root=Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='browser-compose-') as directory:
        Path(directory,'.env.hostinger').write_text('APP_LOGIN_PASSWORD=fake-test-only-password\n')
        config=json.loads(docker('compose','--project-directory',directory,
            '-f',str(root/'compose.hostinger.yaml'),'-f',str(root/'compose.browser.yaml'),'config','--format','json'))
        worker=config['services']['browser']
        assert not worker.get('ports') and not worker.get('env_file')
        assert set(worker['environment'])=={'BROWSER_TOKEN_FILE'}
        assert all(item['target']!='/data' for item in worker['volumes'])
        assert config['services']['agent']['environment']['BILLING_BROWSER_ENABLED']=='true'
        assert config['networks']['browser_private']['internal'] is True
    print('PASS: Compose aislado, sin puertos públicos ni credenciales fiscales en el navegador.')
    volume='billing-browser-test-'+uuid.uuid4().hex[:12]
    container=None
    docker('volume','create',volume)
    common=['--user','10001:10001','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true',
        '--pids-limit=256','--memory=768m','--tmpfs','/tmp:rw,nosuid,nodev,size=536870912,uid=10001,gid=10001,mode=1770']
    try:
        docker('run','--rm','--network','none',*common,'-v',volume+':/run/browser-auth',
            'prueba-codex-browser:local','python','-m','backend.browser_worker.initialize')
        container=docker('run','--rm','-d','--init',*common,'-p','127.0.0.1::8001',
            '-v',volume+':/run/browser-auth:ro','prueba-codex-browser:local')
        port=docker('port',container,'8001').split(':')[-1]
        base='http://127.0.0.1:'+port
        for _ in range(100):
            try:
                with urllib.request.urlopen(base+'/health',timeout=1) as response:
                    assert json.load(response)['public_only'] is True
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError('El servicio de navegador no inició.')
        try:
            urllib.request.urlopen(urllib.request.Request(base+'/v1/sessions',data=b'{}',headers={'Content-Type':'application/json'}),timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code==403
        else:
            raise AssertionError('El servicio permitió una petición sin credencial.')
        token=docker('exec',container,'cat','/run/browser-auth/token')
        request=urllib.request.Request(base+'/v1/sessions',data=json.dumps({
            'owner':'test-owner','url':'file:///etc/passwd','allowed':['https://fixture.example']}).encode(),
            headers={'Content-Type':'application/json','X-Browser-Key':token})
        try:
            urllib.request.urlopen(request,timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code==422
        else:
            raise AssertionError('No rechazó una dirección local.')
        result=subprocess.run(['docker','exec','-i',container,'python','-'],input=CHECK,text=True,capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr[-1800:])
        print(result.stdout.strip())
        print('PASS: /health, autenticación interna y rechazo de archivos locales.')
    finally:
        if container:
            docker('stop',container)
            for _ in range(30):
                check=subprocess.run(['docker','container','inspect',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                if check.returncode:
                    break
                time.sleep(.1)
        docker('volume','rm',volume)
    print('PASS: contenedor y volumen de prueba eliminados.')


if __name__=='__main__':
    main()
