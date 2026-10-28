"""Recorrido autorizado de una vez: SDK y Chromium reales, sitios locales simulados."""
import json
import time

import pytest
import httpx2

from backend.app.billing.portal_navigation import next_public_action
from backend.app.errors import AppError
from backend.browser_worker.engine import BrowserManager
from backend.browser_worker.policy import BrowserError,PublicFetcher
from .billing_fakes import FISCAL,image
from .fake_model import ScriptedModel,answer,call
from .test_adaptive_browser import FakeBrowser,grant,snapshot
from .test_billing import billing_client,ticket
from .test_browser_worker import Pages
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile


class RouteBrowser(FakeBrowser):
    def __init__(self):
        super().__init__()
        self.position=0

    async def open(self,owner,url,*,allowed=None,public_route=False):
        assert public_route and allowed==['https://merchant.example']
        self.calls.append(('open',owner,url))
        return snapshot(actions=[{'reference':'bbbbbbbb','label':'Facturación electrónica','kind':'link'}])

    async def act(self,owner,session,observed,reference):
        self.calls.append(('act',owner,reference))
        self.position+=1
        if self.position==1:
            result=snapshot(actions=[
                {'reference':'cccccccc','label':'Leña Pizza','kind':'link','brand':True,'permission_origin':'https://provider.example'},
                {'reference':'dddddddd','label':'Otra marca','kind':'link','brand':True}],observed='eeeeeeee')
            result['page']='https://merchant.example/invoices'
            return result
        if self.position==2:
            assert reference=='cccccccc'
            result=snapshot(actions=[{'reference':'ffffffff','label':'Continuar al portal de facturación','kind':'link'}],observed='11111111')
            result['page']='https://provider.example/'
            return result
        assert self.position==3 and reference=='ffffffff'
        result=snapshot(fields=[{'label':'Cuenta','key':'account_number','kind':'input','required':True,'options':[],
            'source_url':'https://provider.example/form'}],observed='22222222')
        result.update(page='https://provider.example/form',steps_remaining=3)
        return result


def test_one_native_permission_reaches_form_via_billing_brand_and_provider_without_more_ai(billing_client):
    browser=RouteBrowser()
    model=ScriptedModel([[call('portal_browser_observe')],[answer('Formulario encontrado.')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser)
    confirm_profile(telegram,chat)
    extractor.ticket.update(merchant='LENA PIZZA',billing_url='https://merchant.example/')
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    assert 'seguir su ruta pública' in telegram.messages[-1][1]
    before=len(telegram.messages)
    chat(data=button(telegram,'wa'))
    assert [entry[0] for entry in browser.calls]==['open','act','act','act']
    assert len(telegram.messages)==before+1 and len(model.inputs)==1
    assert 'Cuenta: Pendiente' in telegram.messages[-1][1]
    assert 'Guardar portal' in str(telegram.messages[-1][2])
    assert 'Permitir' not in str(telegram.messages[-1][2])
    assert app.state.database.usage(owner(app))['runs']==1
    assert all(value not in json.dumps(model.inputs) for value in (FISCAL['rfc'],FISCAL['name'],'cliente@example.com'))
    assert mail.sent==sat.calls==[] and len(extractor.calls)==2


def test_automatic_route_has_a_hard_click_budget_and_never_opens_another_ai_job(billing_client):
    class LongRoute(RouteBrowser):
        async def act(self,owner,session,observed,reference):
            self.calls.append(('act',owner,reference))
            self.position+=1
            result=snapshot(actions=[{'reference':'cccccccc','label':'Continuar a facturación','kind':'link'}],observed='dddddddd')
            result['page']='https://merchant.example/step/'+str(self.position)
            return result
    browser=LongRoute()
    model=ScriptedModel([[call('portal_browser_observe')],
        [call('portal_browser_navigate',observation_id='dddddddd',reference='cccccccc')],
        [answer('Revisión pausada.')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser)
    confirm_profile(telegram,chat)
    extractor.ticket['billing_url']='https://merchant.example/'
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    chat(data=button(telegram,'wa'))
    assert len(browser.calls)==8 and browser.position==7
    assert 'step_limit_reached' in str(model.inputs[-1])
    assert app.state.database.usage(owner(app))['runs']==1 and len(model.inputs)==3
    assert mail.sent==sat.calls==[]


def test_old_exact_domain_permission_is_not_silently_upgraded_and_route_is_owned_expiring(billing_client):
    client,app,extractor,*_=billing_client(browser=FakeBrowser())
    extractor.ticket['billing_url']='https://merchant.example/'
    item=ticket(client)
    billing=app.state.billing;permissions=billing.portal_permissions
    grant(client,item['id'])
    assert not permissions.route_allowed('owner',item)
    with pytest.raises(AppError,match='portal_permission'):
        permissions.require('owner',item,'https://provider.example')
    review=permissions.request('owner',item['id'],route=True)
    assert review['route_version']==1
    permissions.decide('owner',review['id'],approve=True)
    assert permissions.route_allowed('owner',item)
    assert not permissions.route_allowed('other',item)
    assert not permissions.route_allowed('owner',item|{'id':'another-ticket'})
    permissions.require('owner',item,'https://provider.example')
    with billing.database.connection(True) as connection:
        connection.execute("UPDATE billing_records SET expires=? WHERE kind='portal_grant'",(time.time()-1,))
    assert not permissions.route_allowed('owner',item)


def test_automatic_choice_stops_for_ambiguous_brand_captcha_form_or_duplicate_links():
    actions=[{'reference':'bbbbbbbb','label':'Cocina Alfa','kind':'link','brand':True},
        {'reference':'cccccccc','label':'Cocina Beta','kind':'link','brand':True}]
    for changes in ({},{'captcha':True},{'network_blocked':True},{'steps_remaining':0}):
        assert next_public_action({'merchant':'Cocina Otra'},snapshot(actions=actions)|changes) is None
    assert next_public_action({'merchant':'Cocina Alfa'},snapshot(actions=actions))==actions[0]
    links=[{'reference':'bbbbbbbb','label':'Facturación','kind':'link'}]*2
    assert next_public_action({},snapshot(actions=links)) is None


async def test_chromium_route_follows_observed_provider_and_loads_functional_resources_without_tracking():
    root='https://merchant.example/'
    provider='https://provider.example/form'
    script='https://cdn.jsdelivr.net/public-form.js'
    stylesheet='https://fonts.googleapis.com/css?family=Test'
    tracker='https://www.googletagmanager.com/gtm.js'
    pages=Pages({root:'<a href="'+provider+'"><img alt="Leña Pizza" src="/logo.png"></a>',
        provider:'<body><link rel="stylesheet" href="'+stylesheet+'"><script src="'+tracker+'"></script><script src="'+script+'"></script>'})
    original=pages.fetch
    async def fetch(url,headers):
        if url==script:
            pages.calls.append((url,headers))
            return 200,{'content-type':'application/javascript'},b'document.body.innerHTML="<label>RFC<input name=rfc></label>"'
        if url==stylesheet:
            pages.calls.append((url,headers))
            return 200,{'content-type':'text/css'},b'body {color: black}'
        return await original(url,headers)
    pages.fetch=fetch
    manager=BrowserManager(fetcher=pages)
    try:
        first=await manager.open('owner',root,['https://merchant.example'],public_route=True)
        assert not first['actions'][0].get('permission_origin')
        current=await manager.act('owner',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
        assert current['fields'][0]['label']=='RFC'
        assert current['blocked_origins']==[] and not current['network_blocked']
        fetched=[url for url,_ in pages.calls]
        assert script in fetched and stylesheet in fetched and tracker not in fetched
        with pytest.raises(BrowserError):
            await manager.observe('other-user',first['session_id'])
    finally:
        await manager.close()


async def test_route_resources_follow_validated_redirect_without_forwarding_prior_cookie():
    root='https://merchant.example/'
    source='https://merchant.example/form.js'
    target='https://cdn.example/form.js'
    class Redirects(Pages):
        async def fetch(self,url,headers):
            if url==root:
                self.calls.append((url,headers))
                return 200,{'content-type':'text/html','set-cookie':'private-public-session=yes; Path=/; Secure'},b'<body><script src="/form.js"></script>'
            if url==source:
                self.calls.append((url,headers))
                return 302,{'location':target},b''
            if url==target:
                self.calls.append((url,headers))
                return 200,{'content-type':'application/javascript'},b'document.body.innerHTML="<label>Cuenta<input name=account></label>"'
            return await super().fetch(url,headers)
    pages=Redirects({})
    manager=BrowserManager(fetcher=pages)
    try:
        observed=await manager.open('owner',root,['https://merchant.example'],public_route=True)
        assert observed['fields'][0]['label']=='Cuenta'
        headers=dict(pages.calls)
        assert 'private-public-session=yes' in headers[source].get('cookie','')
        assert not headers[target].get('cookie')
    finally:
        await manager.close()


async def test_route_authorization_cannot_fetch_private_dns_or_submit_post():
    requests=[]
    async def resolver(host):
        return ['127.0.0.1'] if host=='private.example' else ['93.184.216.34']
    def transport(request):
        requests.append(request)
        assert request.headers['host']=='merchant.example'
        return httpx2.Response(200,headers={'content-type':'text/html'},text='''<body>
          <iframe src="https://private.example/"></iframe>
          <script>fetch('https://provider.example/submit',{method:'POST',body:'never-send'}).catch(()=>{});</script>
        ''')
    fetcher=PublicFetcher(resolver=resolver,client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)))
    manager=BrowserManager(fetcher=fetcher)
    try:
        observed=await manager.open('owner','https://merchant.example/',['https://merchant.example'],public_route=True)
        assert not observed['network_blocked'] and not observed['submitted']
        assert observed['omitted_writes']==1
        assert len(requests)==1 and all(request.method=='GET' for request in requests)
    finally:
        await manager.close()


async def test_route_origin_limit_is_enforced_without_offering_unbounded_new_grants():
    root='https://merchant.example/'
    scripts=''.join('<script src="https://cdn'+str(index)+'.example/form.js"></script>' for index in range(14))
    manager=BrowserManager(fetcher=Pages({root:scripts}))
    try:
        observed=await manager.open('owner',root,['https://merchant.example'],public_route=True)
        session=await manager.get('owner',observed['session_id'])
        assert len(session.allowed)==12 and session.network_blocked and session.unsafe_blocked
        assert observed['block_reason']=='origin_limit'
        assert len(manager.fetcher.calls)<=12
    finally:
        await manager.close()
