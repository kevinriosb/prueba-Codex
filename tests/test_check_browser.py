import json
import socket
import ssl

import httpx2
import pytest

from backend.browser_worker.policy import PublicFetcher
from scripts.check_browser import category,check_chromium,check_connection,check_portal


class Reply:
    def __init__(self,status,body):
        self.code=status
        self.body=json.dumps(body).encode()
    def read(self,maximum):
        return self.body[:maximum]
    def __enter__(self):
        return self
    def __exit__(self,*args):
        pass


class Opener:
    def __init__(self,*replies):
        self.replies=list(replies)
        self.requests=[]
    def open(self,request,timeout):
        self.requests.append(request)
        return self.replies.pop(0)


def environment(tmp_path):
    token=tmp_path/'private-token'
    token.write_text('fake-private-internal-key-'+'x'*40)
    return {'BILLING_BROWSER_WORKER_URL':'http://browser:8001','BILLING_BROWSER_TOKEN_FILE':str(token),
        'AI_API_KEY':'do-not-print-ai-key','BILLING_ENCRYPTION_KEY':'do-not-print-encryption-key'}


def test_agent_probe_confirms_internal_auth_without_starting_browser_or_sending_fiscal_data(tmp_path):
    config=environment(tmp_path)
    probe=Opener(Reply(200,{'status':'ok','public_only':True}),Reply(422,{'code':'browser_url'}))
    output=[]
    assert check_connection(config,opener=probe,emit=output.append)==0
    assert len(probe.requests)==2
    assert probe.requests[0].get_header('X-browser-key') is None
    assert probe.requests[1].get_header('X-browser-key')==('fake-private-internal-key-'+'x'*40)
    assert json.loads(probe.requests[1].data)=={
        'owner':'diagnostic-no-user','url':'file:///diagnostic-not-opened','allowed':['https://diagnostic.invalid']}
    assert all(value not in '\n'.join(output) for value in config.values())


@pytest.mark.parametrize('status,phrase',[(403,'rechazada'),(404,'misma actualización'),(500,'misma actualización')])
def test_agent_probe_reports_failure_without_printing_worker_body_or_secret(tmp_path,status,phrase):
    config=environment(tmp_path)
    probe=Opener(Reply(200,{'status':'ok','public_only':True}),Reply(status,{'message':'private-secret-body'}))
    output=[]
    assert check_connection(config,opener=probe,emit=output.append)==1
    assert phrase in output[-1]
    assert 'private-secret-body' not in str(output)
    assert 'fake-private-internal-key-' not in str(output)


def test_agent_probe_does_not_send_key_to_an_unexpected_health_service(tmp_path):
    probe=Opener(Reply(200,{'status':'ok'}))
    output=[]
    assert check_connection(environment(tmp_path),opener=probe,emit=output.append)==1
    assert len(probe.requests)==1 and probe.requests[0].get_header('X-browser-key') is None


def test_missing_private_volume_is_reported_without_traceback_or_file_contents(tmp_path):
    config=environment(tmp_path)
    (tmp_path/'private-token').unlink()
    output=[]
    probe=Opener(Reply(200,{'status':'ok','public_only':True}))
    assert check_connection(config,opener=probe,emit=output.append)==1
    assert 'browser_auth' in output[-1] and len(probe.requests)==1


@pytest.mark.parametrize('error,expected',[
    (socket.gaierror('private-request-details'),'dns'),
    (ssl.SSLCertVerificationError('private-certificate-details'),'tls'),
    (httpx2.ConnectTimeout('private-url'),'timeout'),
    (httpx2.ReadTimeout('private-url'),'timeout'),
    (httpx2.ConnectError('private-url'),'connection'),
    (ValueError('private-unexpected-details'),'unknown'),
])
def test_only_fixed_safe_failure_categories_are_returned(error,expected):
    assert category(error)==expected


async def test_real_chromium_local_probe_never_calls_a_public_fetcher(monkeypatch):
    async def forbidden(*args):
        pytest.fail('La prueba de Chromium no debe consultar comercios')
    monkeypatch.setattr(PublicFetcher,'fetch',forbidden)
    output=[]
    assert await check_chromium(emit=output.append)==0
    assert 'correctos' in output[-1]


async def public_dns(host):
    return ['93.184.216.34']


def factory(handler,*,resolver=public_dns):
    return lambda:PublicFetcher(resolver=resolver,client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))


async def test_https_probe_only_performs_one_get_without_query_fragment_credentials_or_body():
    requests=[]
    def transport(request):
        requests.append(request)
        return httpx2.Response(200,text='private-page-body-not-to-print')
    output=[]
    assert await check_portal('http://merchant.example/public?customer=private-query#private-fragment',
        fetcher_factory=factory(transport),emit=output.append)==0
    assert len(requests)==1 and requests[0].method=='GET' and not requests[0].content
    assert requests[0].url.scheme=='https' and requests[0].url.path=='/public' and not requests[0].url.query
    assert requests[0].headers['host']=='merchant.example'
    assert 'authorization' not in requests[0].headers and requests[0].headers['cookie']==''
    assert all(value not in str(output) for value in ['private-query','private-fragment','private-page-body'])


@pytest.mark.parametrize('status',[301,302,303,307,308])
async def test_redirect_is_not_followed_and_only_destination_origin_is_shown(status):
    requests=[]
    def transport(request):
        requests.append(request)
        return httpx2.Response(status,headers={'location':'https://billing.example/private-path?token=private-token'})
    output=[]
    assert await check_portal('https://merchant.example/',fetcher_factory=factory(transport),emit=output.append)==0
    assert len(requests)==1 and 'https://billing.example' in output[-1]
    assert 'No se abrió el destino' in output[-1]
    assert 'private-' not in str(output)


@pytest.mark.parametrize('status,phrase',[(403,'rechazó'),(404,'no existe'),(429,'limitó'),(502,'sin éxito')])
async def test_https_error_status_has_a_fixed_explanation(status,phrase):
    output=[]
    assert await check_portal('https://merchant.example/',
        fetcher_factory=factory(lambda _:httpx2.Response(status,text='private-body')),emit=output.append)==1
    assert phrase in output[-1] and 'private-body' not in str(output)


async def test_private_dns_is_rejected_without_any_http_or_chromium_public_request():
    async def private_dns(host):
        return ['127.0.0.1']
    def forbidden(request):
        pytest.fail('No debe conectar con la IP privada')
    output=[]
    assert await check_portal('https://merchant.example/',
        fetcher_factory=factory(forbidden,resolver=private_dns),emit=output.append)==1
    assert 'direcciones privadas' in output[-1]


@pytest.mark.parametrize('value',['file:///etc/passwd','https://127.0.0.1/','https://user:private-key@merchant.example/'])
async def test_unsafe_url_is_rejected_before_constructing_fetcher(value):
    def forbidden():
        pytest.fail('No debe crear el transporte')
    output=[]
    assert await check_portal(value,fetcher_factory=forbidden,emit=output.append)==1
    assert 'private-key' not in str(output) and 'dirección no permitida' in output[-1]


@pytest.mark.parametrize('cause,phrase',[
    (socket.gaierror('private-dns-details'),'DNS:'),
    (ssl.SSLCertVerificationError('private-tls-details'),'TLS:'),
    (httpx2.ConnectTimeout('private-timeout-details'),'tiempo de espera'),
])
async def test_wrapped_transport_failures_are_classified_without_exposing_exception(cause,phrase):
    def transport(request):
        raise httpx2.ConnectError('private-url-or-header-details') from cause
    output=[]
    assert await check_portal('https://merchant.example/',fetcher_factory=factory(transport),emit=output.append)==1
    assert phrase in output[-1] and 'private-' not in str(output)
