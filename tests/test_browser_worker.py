import asyncio
import json
import time

import httpx2
import pytest
from fastapi.testclient import TestClient

from backend.browser_worker.engine import BrowserManager
from backend.browser_worker.policy import BrowserError,PublicFetcher,https_url
from backend.browser_worker.app import create_worker


HOME='https://merchant.example/'
FORM='https://billing.example/form'
ALLOWED=['https://merchant.example','https://billing.example']


class Pages:
    def __init__(self,pages):
        self.pages=pages
        self.calls=[]
    async def fetch(self,url,headers):
        self.calls.append((url,headers))
        return 200,{'content-type':'text/html; charset=utf-8'},self.pages.get(url,'<p>Página pública</p>').encode()
    async def close(self):
        pass


@pytest.fixture
async def browser():
    pages=Pages({HOME:'<a href="https://billing.example/form">Facturación</a>',
        FORM:'<label>Cuenta<input name="account"></label>'})
    manager=BrowserManager(fetcher=pages)
    yield manager,pages
    await manager.close()


async def test_real_chromium_follows_observed_link_and_discovers_account_without_private_data(browser):
    manager,pages=browser
    initial=await manager.open('owner-a',HOME,ALLOWED)
    assert initial['fields']==[] and initial['actions'][0]['label']=='Facturación'
    next_page=await manager.act('owner-a',initial['session_id'],initial['observation_id'],initial['actions'][0]['reference'])
    assert next_page['fields'][0]['label']=='Cuenta'
    assert next_page['page']==FORM and not next_page['submitted'] and not next_page['purchase_validated']
    assert all('authorization' not in headers for _,headers in pages.calls)
    assert next_page['network_blocked'] is False


async def test_http_link_is_followed_only_over_https_on_approved_host(browser):
    manager,pages=browser
    pages.pages[HOME]='<a href="http://billing.example/form">Facturación</a>'
    first=await manager.open('owner-a',HOME,ALLOWED)
    after=await manager.act('owner-a',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
    assert after['page']==FORM and all(url.startswith('https://') for url,_ in pages.calls)


async def test_different_page_expands_alternative_and_discovers_toll_fields(browser):
    manager,pages=browser
    pages.pages[HOME]='''<label>Web ID<input></label>
      <button aria-expanded="false" onclick="document.getElementById('details').hidden=false">No cuento con Web ID</button>
      <div id="details" hidden><label>Folio<input required></label><label>Caseta<select><option>Caseta A</option></select></label>
      <label>Carril<input></label><label>Fecha<input type="date"></label><label>Hora<input type="time"></label></div>'''
    initial=await manager.open('owner-a',HOME,ALLOWED)
    assert [item['label'] for item in initial['fields']]==['Web ID']
    expanded=await manager.act('owner-a',initial['session_id'],initial['observation_id'],initial['actions'][0]['reference'])
    assert [item['label'] for item in expanded['fields']]==['Web ID','Folio','Caseta','Carril','Fecha','Hora']
    assert next(item for item in expanded['fields'] if item['label']=='Folio')['required'] is True
    assert expanded['fields'][2]['options']==['Caseta A']


async def test_third_layout_reads_reference_and_excludes_hidden_values_and_submit_controls(browser):
    manager,pages=browser
    pages.pages[HOME]='''<form action="/issue" method="post"><input type="hidden" value="private-hidden-nonce">
      <label for="ref">Referencia de compra</label><input id="ref" value="not-returned">
      <label>RFC<input type="password" value="not-returned-either"></label>
      <button type="submit">Emitir factura</button></form>'''
    observed=await manager.open('owner-a',HOME,ALLOWED)
    assert [item['label'] for item in observed['fields']]==['Referencia de compra']
    assert observed['actions']==[]
    assert 'not-returned' not in json.dumps(observed) and 'private-hidden' not in json.dumps(observed)


async def test_old_reference_and_foreign_owner_never_click(browser):
    manager,pages=browser
    initial=await manager.open('owner-a',HOME,ALLOWED)
    with pytest.raises(BrowserError) as error:
        await manager.act('owner-b',initial['session_id'],initial['observation_id'],initial['actions'][0]['reference'])
    assert error.value.status==404 and len(pages.calls)==1
    await manager.act('owner-a',initial['session_id'],initial['observation_id'],initial['actions'][0]['reference'])
    calls=len(pages.calls)
    with pytest.raises(BrowserError,match='browser_stale'):
        await manager.act('owner-a',initial['session_id'],initial['observation_id'],initial['actions'][0]['reference'])
    assert len(pages.calls)==calls


async def test_unapproved_redirect_and_script_post_are_blocked_before_fetch(browser):
    manager,pages=browser
    pages.pages[HOME]='''<button aria-expanded="false" onclick="fetch('/issue',{method:'POST',body:'test-only'}).catch(()=>{})">Mostrar opciones</button>'''
    first=await manager.open('owner-a',HOME,ALLOWED)
    after=await manager.act('owner-a',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
    await asyncio.sleep(.05)
    assert all('/issue' not in url for url,_ in pages.calls)
    assert manager.sessions[first['session_id']].network_blocked is True


async def test_session_step_limit_and_expiry_do_not_restart(browser):
    manager,pages=browser
    manager.max_steps=1
    initial=await manager.open('owner-a',HOME,ALLOWED)
    with pytest.raises(BrowserError,match='browser_steps'):
        await manager.act('owner-a',initial['session_id'],initial['observation_id'],initial['actions'][0]['reference'])
    manager.sessions[initial['session_id']].created-=200
    with pytest.raises(BrowserError,match='browser_session'):
        await manager.observe('owner-a',initial['session_id'])


async def test_browser_contexts_have_no_shared_cookies(browser):
    manager,pages=browser
    pages.pages[HOME]='<script>document.cookie="publicMarker=ownerA"</script>'
    a=await manager.open('owner-a',HOME,ALLOWED)
    pages.pages[HOME]='<label>Folio<input></label>'
    b=await manager.open('owner-b',HOME,ALLOWED)
    assert await manager.sessions[a['session_id']].context.cookies()
    assert await manager.sessions[b['session_id']].context.cookies()==[]


@pytest.mark.parametrize('url',['http://public.example','file:///etc/passwd','https://127.0.0.1/',
    'https://user:pass@public.example/','https://public.example:8443/','https://host.local/'])
def test_forbidden_browser_urls(url):
    with pytest.raises(BrowserError):
        https_url(url)


async def test_network_fetch_pins_public_ip_and_keeps_tls_host_without_credentials():
    requests=[]
    def transport(request):
        requests.append(request)
        return httpx2.Response(200,text='<p>Public</p>')
    async def resolver(host):
        return ['93.184.216.34']
    fetcher=PublicFetcher(resolver=resolver,client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)))
    try:
        await fetcher.fetch(HOME,{'authorization':'never-forward','cookie':'publicOnly=yes'})
        assert requests[0].url.host=='93.184.216.34' and requests[0].headers['host']=='merchant.example'
        assert requests[0].extensions['sni_hostname']=='merchant.example'
        assert 'authorization' not in requests[0].headers
    finally:
        await fetcher.close()


async def test_mixed_private_public_dns_is_rejected_without_http():
    async def resolver(host):
        return ['93.184.216.34','127.0.0.1']
    def transport(request):
        raise AssertionError('No se permite salir a red.')
    fetcher=PublicFetcher(resolver=resolver,client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)))
    try:
        with pytest.raises(BrowserError,match='browser_address'):
            await fetcher.fetch(HOME,{})
    finally:
        await fetcher.close()


def test_worker_auth_health_no_docs_and_rejects_private_payload_fields(tmp_path):
    token=tmp_path/'token';token.write_text('test-worker-key-'+'x'*40)
    class Manager:
        async def purge(self): pass
        async def close(self): pass
    with TestClient(create_worker(Manager(),token)) as client:
        assert client.get('/health').status_code==200
        assert client.post('/v1/sessions',json={}).status_code==403
        headers={'x-browser-key':token.read_text()}
        assert client.get('/docs',headers=headers).status_code==404
        assert client.post('/v1/sessions',headers=headers,json={
            'owner':'owner-a','url':HOME,'allowed':ALLOWED,'rfc':'not-allowed'}).status_code==422
