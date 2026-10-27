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


@pytest.mark.parametrize('approve_auxiliary_first',[False,True])
async def test_approving_main_destination_does_not_require_unrelated_optional_resource(browser,approve_auxiliary_first):
    manager,_=browser
    class RedirectWithOptionalScript(Pages):
        async def fetch(self,url,headers):
            if url=='https://merchant.example/jump':
                self.calls.append((url,headers))
                return 302,{'location':FORM},b''
            return await super().fetch(url,headers)
    manager.fetcher=pages=RedirectWithOptionalScript({
        HOME:'<script src="https://cdn.example/optional.js"></script><a href="/jump">Facturación</a>',
        FORM:'<label>Cuenta<input></label>'})
    first=await manager.open('owner',HOME,[ALLOWED[0]])
    redirected=await manager.act('owner',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
    assert redirected['network_blocked'] is True
    assert set(redirected['blocked_origins'])=={'https://cdn.example',ALLOWED[1]}
    if approve_auxiliary_first:
        partial=await manager.allow('owner',first['session_id'],[ALLOWED[0],'https://cdn.example'])
        assert partial['network_blocked'] is True
        assert not any(url==FORM for url,_ in pages.calls)
    allowed=await manager.allow('owner',first['session_id'],ALLOWED+(['https://cdn.example'] if approve_auxiliary_first else []))
    assert allowed['network_blocked'] is False
    after=await manager.act('owner',allowed['session_id'],allowed['observation_id'],allowed['actions'][0]['reference'])
    assert after['fields'][0]['label']=='Cuenta'
    assert not any(url.startswith('https://cdn.example') for url,_ in pages.calls)


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


async def test_only_the_current_contexts_http_only_cookie_is_forwarded(browser):
    manager,_=browser
    class CookiePages(Pages):
        async def fetch(self,url,headers):
            self.calls.append((url,headers))
            response={'content-type':'text/html; charset=utf-8'}
            if url==HOME:
                self.visitors=getattr(self,'visitors',0)+1
                response['set-cookie']=f'public-session=visitor-{self.visitors}; HttpOnly; Secure; Path=/'
                body='<a href="https://merchant.example/form">Facturación</a>'
            else: body='<label>Folio<input></label>'
            return 200,response,body.encode()
    manager.fetcher=pages=CookiePages({})
    a=await manager.open('owner-a',HOME,ALLOWED)
    b=await manager.open('owner-b',HOME,ALLOWED)
    await manager.act('owner-a',a['session_id'],a['observation_id'],a['actions'][0]['reference'])
    assert pages.calls[-1][1]['cookie']=='public-session=visitor-1'
    await manager.act('owner-b',b['session_id'],b['observation_id'],b['actions'][0]['reference'])
    assert pages.calls[-1][1]['cookie']=='public-session=visitor-2'


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


async def test_shared_transport_never_retains_or_injects_another_contexts_cookie():
    requests=[]
    def transport(request):
        requests.append(request)
        return httpx2.Response(200,text='Public',headers={'set-cookie':'public-session=owner-a; Path=/'})
    async def resolver(host): return ['93.184.216.34']
    fetcher=PublicFetcher(resolver=resolver,client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)))
    try:
        await fetcher.fetch(HOME,{})
        await fetcher.fetch(HOME,{})
        await fetcher.fetch(HOME,{'cookie':'public-session=owner-b'})
        assert requests[1].headers['cookie']==''
        assert requests[2].headers['cookie']=='public-session=owner-b'
        assert len(fetcher.client.cookies)==0
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
        assert client.post('/v1/sessions/test/origins',json={}).status_code==403
        assert client.request('DELETE','/v1/sessions/test',json={'owner':'owner-a'}).status_code==403
        headers={'x-browser-key':token.read_text()}
        assert client.get('/docs',headers=headers).status_code==404
        assert client.post('/v1/sessions',headers=headers,json={
            'owner':'owner-a','url':HOME,'allowed':ALLOWED,'rfc':'not-allowed'}).status_code==422


async def test_observed_cross_domain_link_waits_for_backend_permission_without_fetch(browser):
    manager,pages=browser
    first=await manager.open('owner-a',HOME,[ALLOWED[0]])
    action=first['actions'][0]
    assert action['permission_origin']==ALLOWED[1]
    assert len(pages.calls)==1 and first['blocked_origins']==[]
    with pytest.raises(BrowserError,match='browser_origin'):
        await manager.act('owner-a',first['session_id'],first['observation_id'],action['reference'])
    assert len(pages.calls)==1
    with pytest.raises(BrowserError,match='browser_session'):
        await manager.allow('owner-b',first['session_id'],ALLOWED)
    current=await manager.allow('owner-a',first['session_id'],ALLOWED)
    assert len(pages.calls)==1  # Permitir enlace no lo abre por sí solo.
    assert not current['actions'][0].get('permission_origin')
    after=await manager.act('owner-a',current['session_id'],current['observation_id'],current['actions'][0]['reference'])
    assert after['page']==FORM and len(pages.calls)==2


async def test_redirect_waits_for_new_domain_permission_instead_of_fetching_or_failing_opaquely(browser):
    manager,_=browser
    class RedirectPages(Pages):
        async def fetch(self,url,headers):
            if url==HOME:
                self.calls.append((url,headers))
                return 302,{'location':FORM},b''
            return await super().fetch(url,headers)
    manager.fetcher=pages=RedirectPages({FORM:'<label>Cuenta<input></label>'})
    first=await manager.open('owner-a',HOME,[ALLOWED[0]])
    assert first['blocked_origins']==[ALLOWED[1]] and first['network_blocked']
    assert [url for url,_ in pages.calls]==[HOME]
    current=await manager.allow('owner-a',first['session_id'],ALLOWED)
    assert not current['network_blocked'] and current['blocked_origins']==[]
    assert [url for url,_ in pages.calls]==[HOME,HOME]
    current=await manager.act('owner-a',current['session_id'],current['observation_id'],current['actions'][0]['reference'])
    assert current['page']==FORM and current['fields'][0]['label']=='Cuenta'
    assert [url for url,_ in pages.calls]==[HOME,HOME,FORM]


async def test_missing_resource_domain_can_be_granted_but_never_clears_unsafe_post_block(browser):
    manager,pages=browser
    pages.pages[HOME]='<script src="https://billing.example/app.js"></script><label>Web ID<input></label>'
    pages.pages['https://billing.example/app.js']='document.title="Carga autorizada"'
    first=await manager.open('owner-a',HOME,[ALLOWED[0]])
    assert first['blocked_origins']==[ALLOWED[1]] and len(pages.calls)==1
    after=await manager.allow('owner-a',first['session_id'],ALLOWED)
    assert not after['network_blocked'] and any(url.endswith('app.js') for url,_ in pages.calls)
    pages.pages[HOME]='<script>fetch("/issue",{method:"POST"}).catch(()=>{})</script>'
    await manager.sessions[first['session_id']].page.reload(wait_until='networkidle')
    assert manager.sessions[first['session_id']].unsafe_blocked
    count=len(pages.calls)
    blocked=await manager.allow('owner-a',first['session_id'],ALLOWED)
    assert blocked['network_blocked'] and len(pages.calls)==count
    assert all('/issue' not in url for url,_ in pages.calls)


async def test_close_session_is_owner_bound_and_releases_public_context(browser):
    manager,pages=browser
    first=await manager.open('owner-a',HOME,ALLOWED)
    with pytest.raises(BrowserError,match='browser_session'):
        await manager.close_session('owner-b',first['session_id'])
    assert await manager.close_session('owner-a',first['session_id'])=={'closed':True}
    assert manager.sessions=={} and len(pages.calls)==1


@pytest.mark.parametrize('url,expected',[
    ('https://www.googletagmanager.com/gtm.js?id=FAKE',True),
    ('https://region1.google-analytics.com/g/collect',True),
    ('https://stats.g.doubleclick.net/collect',True),
    ('https://googletagmanager.com.evil.example/script.js',False),
    ('https://notgoogletagmanager.com/script.js',False),
    ('https://ajax.googleapis.com/jquery.js',False),
    ('https://www.google.com/recaptcha/api.js',False),
    ('https://billing.example/form',False),
])
def test_tracking_host_filter_uses_hostname_boundaries_without_blocking_functional_providers(url,expected):
    from backend.browser_worker.policy import tracking_url
    assert tracking_url(url) is expected


async def test_tracking_scripts_never_fetch_or_request_permission_and_billing_footer_is_prioritized(browser):
    manager,pages=browser
    pages.pages[HOME]='''<script src="https://www.googletagmanager.com/gtm.js?id=FAKE"></script>
      <script>fetch('https://region1.google-analytics.com/g/collect',{method:'POST',body:'fake'}).catch(()=>{})</script>'''
    pages.pages[HOME]+=''.join('<a href="https://merchant.example/page/'+str(index)+'">Información '+str(index)+'</a>' for index in range(40))
    pages.pages[HOME]+='<footer><a href="https://billing.example/form">Facturación electrónica</a></footer>'
    first=await manager.open('owner-a',HOME,[ALLOWED[0]])
    assert [url for url,_ in pages.calls]==[HOME]
    assert first['blocked_origins']==[] and not first['network_blocked']
    assert not manager.sessions[first['session_id']].unsafe_blocked
    assert first['actions'][0]['label']=='Facturación electrónica'
    assert first['actions'][0]['permission_origin']==ALLOWED[1]
    current=await manager.allow('owner-a',first['session_id'],ALLOWED)
    after=await manager.act('owner-a',current['session_id'],current['observation_id'],current['actions'][0]['reference'])
    assert after['page']==FORM and after['fields'][0]['label']=='Cuenta'
    assert [url for url,_ in pages.calls]==[HOME,FORM]


async def test_even_previously_approved_tracking_resources_are_omitted_without_blocking_navigation(browser):
    manager,pages=browser
    pages.pages[HOME]='<script src="https://www.googletagmanager.com/gtm.js"></script><a href="/form">Facturar</a>'
    first=await manager.open('owner-a',HOME,[*ALLOWED,'https://www.googletagmanager.com'])
    assert not first['network_blocked'] and first['blocked_origins']==[]
    assert [url for url,_ in pages.calls]==[HOME]


async def test_ancillary_unapproved_resource_does_not_block_an_observed_link_on_approved_domain(browser):
    manager,pages=browser
    pages.pages[HOME]='<script src="https://cdn.example/ui.js"></script><a href="/form">Facturación</a>'
    pages.pages['https://merchant.example/form']='<label>Folio<input></label>'
    first=await manager.open('owner-a',HOME,[ALLOWED[0]])
    assert first['blocked_origins']==['https://cdn.example']
    assert not first['network_blocked'] and [url for url,_ in pages.calls]==[HOME]
    after=await manager.act('owner-a',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
    assert after['fields'][0]['label']=='Folio' and not after['network_blocked']
    assert all('cdn.example' not in url for url,_ in pages.calls)


async def test_failed_auxiliary_stylesheet_does_not_force_human_intervention(browser):
    manager,_=browser
    class FailedStyle(Pages):
        async def fetch(self,url,headers):
            if url.endswith('.css'):
                raise httpx2.ConnectTimeout('not-to-print')
            return await super().fetch(url,headers)
    manager.fetcher=pages=FailedStyle({HOME:'<link rel="stylesheet" href="/optional.css"><a href="/form">Facturación</a>',
        'https://merchant.example/form':'<label>Cuenta<input></label>'})
    first=await manager.open('owner-a',HOME,ALLOWED)
    assert not first['network_blocked']
    after=await manager.act('owner-a',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
    assert after['fields'][0]['label']=='Cuenta'


async def test_missing_public_frame_resource_does_not_stop_main_page_but_requires_own_permission(browser):
    manager,pages=browser
    pages.pages[HOME]='<iframe src="https://cdn.example/embed"></iframe><a href="/form">Facturar</a>'
    first=await manager.open('owner-a',HOME,[ALLOWED[0]])
    assert first['blocked_origins']==['https://cdn.example'] and not first['network_blocked']
    assert all('cdn.example' not in url for url,_ in pages.calls)


@pytest.mark.parametrize('image_markup,expected',[
    ('<img src="/brand.png" alt="Domino’s">','Domino’s'),
    ('<img src="/brand.png" title="Logo Domino-s">','Logo Domino-s'),
    ('<img src="/images/logo-dominos.svg">','logo dominos'),
])
async def test_brand_image_links_are_named_without_downloading_images_and_keep_domain_permission(browser,image_markup,expected):
    manager,pages=browser
    pages.pages[HOME]='<a href="https://billing.example/form" target="_blank">'+image_markup+'</a>'
    first=await manager.open('owner-a',HOME,[ALLOWED[0]])
    assert first['actions'][0]['label']==expected and first['actions'][0]['brand']
    assert first['actions'][0]['permission_origin']==ALLOWED[1]
    assert [url for url,_ in pages.calls]==[HOME]


@pytest.mark.parametrize('markup',[
    '<button onclick="window.open(\'https://billing.example/form\',\'_blank\')"><img alt="Dominos"></button>',
    '<a href="#" onclick="location.href=\'https://billing.example/form\';"><img alt="Dominos"></a>',
    '<img alt="Dominos" width="100" height="50" onclick="window.location.assign(\'https://billing.example/form\')">',
])
async def test_literal_observed_navigation_is_followed_as_a_get_without_executing_page_handler(browser,markup):
    manager,pages=browser
    pages.pages[HOME]=markup
    first=await manager.open('owner-a',HOME,ALLOWED)
    assert first['actions'][0]['label']=='Dominos'
    after=await manager.act('owner-a',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
    assert after['page']==FORM and after['fields'][0]['source_url']==FORM
    assert len(manager.sessions[first['session_id']].context.pages)==1
    assert [url for url,_ in pages.calls]==[HOME,FORM]


async def test_arbitrary_onclick_and_form_buttons_are_not_exposed_as_navigation(browser):
    manager,pages=browser
    pages.pages[HOME]='''<button onclick="fetch('/issue',{method:'POST'});window.open('https://billing.example/form')"><img alt="Dominos"></button>
     <form id="issue"><button onclick="window.open('https://billing.example/form')"><img alt="Otra marca"></button></form>
     <button form="issue" onclick="window.open('https://billing.example/form')"><img alt="Marca fuera de formulario"></button>'''
    first=await manager.open('owner-a',HOME,ALLOWED)
    assert first['actions']==[] and [url for url,_ in pages.calls]==[HOME]


async def test_iframe_form_exposes_its_stable_public_source_without_private_values(browser):
    manager,pages=browser
    pages.pages[HOME]='<iframe src="https://billing.example/form?brand=Demo"></iframe>'
    pages.pages['https://billing.example/form?brand=Demo']='<label>RFC<input value="private-input-never-returned"></label>'
    first=await manager.open('owner-a',HOME,ALLOWED)
    assert first['fields'][0]['source_url']=='https://billing.example/form?brand=Demo'
    assert 'private-input-never-returned' not in json.dumps(first)


@pytest.mark.parametrize('url,expected',[
    ('https://billing.example/form?opc=Dominos','https://billing.example/form?opc=Dominos'),
    ('https://billing.example/form?brand=Demo&utm_source=ads#section','https://billing.example/form?brand=Demo'),
    ('https://billing.example/form?token=private',None),
    ('https://billing.example/form?session=private',None),
    ('https://billing.example/form?folio=private',None),
    ('https://billing.example/form?brand=abc%40private.example',None),
    ('https://billing.example/form;jsessionid=private',None),
    ('https://127.0.0.1/form',None),
    ('https://user:private@billing.example/form',None),
    ('https://www.googletagmanager.com/gtm.js',None),
])
def test_only_stable_public_brand_routes_are_eligible_for_bookmarks(url,expected):
    from backend.browser_worker.policy import bookmark_url
    assert bookmark_url(url)==expected
