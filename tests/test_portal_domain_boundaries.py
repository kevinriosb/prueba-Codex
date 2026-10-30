"""La navegación documental exige permiso por dominio, incluso con recorrido público."""
import time

import pytest

from backend.browser_worker.engine import BrowserManager
from backend.browser_worker.policy import BrowserError
from .test_browser_worker import Pages
from .test_billing import billing_client
from .test_adaptive_browser import FakeBrowser,snapshot,grant
from .test_telegram_agent import create,owner,button
from .test_telegram_documents import confirm_profile
from .billing_fakes import image
from .fake_model import ScriptedModel,call


ROOT='https://hub.example/'
DEST='https://provider.example/form'
FORM='<form><label>RFC<input required></label><label>Ticket<input maxlength=9 required></label><label>Tienda<input maxlength=5 required></label><label>Fecha<input type=date required></label></form>'


async def test_cross_domain_link_is_not_fetched_or_observed_until_explicitly_granted():
    pages=Pages({ROOT:'<a href="'+DEST+'"><img alt="Cocina Aurora" src="/image.png"></a>',DEST:FORM})
    manager=BrowserManager(fetcher=pages)
    try:
        first=await manager.open('owner',ROOT,['https://hub.example'],public_route=True)
        action=first['actions'][0]
        assert action['permission_origin']=='https://provider.example'
        with pytest.raises(BrowserError,match='browser_origin'):
            await manager.act('owner',first['session_id'],first['observation_id'],action['reference'])
        assert [url for url,_ in pages.calls]==[ROOT]
        observed=await manager.allow('owner',first['session_id'],['https://hub.example','https://provider.example'],public_route=True)
        observed=await manager.act('owner',observed['session_id'],observed['observation_id'],observed['actions'][0]['reference'])
        assert [item['label'] for item in observed['fields']]==['RFC','Ticket','Tienda','Fecha']
        assert [item['max_length'] for item in observed['fields']][1:3]==[9,5]
        assert not observed['submitted']
    finally:await manager.close()


@pytest.mark.parametrize('kind',['redirect','iframe','javascript'])
async def test_implicit_cross_domain_navigation_stops_before_fetching_the_destination(kind):
    class Redirect(Pages):
        async def fetch(self,url,headers):
            if kind=='redirect' and url==ROOT:
                self.calls.append((url,headers));return 302,{'location':DEST},b''
            return await super().fetch(url,headers)
    html='<iframe src="'+DEST+'"></iframe>' if kind=='iframe' else '<script>location.href="'+DEST+'"</script>'
    pages=Redirect({ROOT:html,DEST:FORM});manager=BrowserManager(fetcher=pages)
    try:
        observed=await manager.open('owner',ROOT,['https://hub.example'],public_route=True)
        assert 'https://provider.example' in observed['blocked_origins']
        assert not observed['fields'] and DEST not in [url for url,_ in pages.calls]
        assert 'https://provider.example' not in (await manager.get('owner',observed['session_id'])).allowed
    finally:await manager.close()


def test_native_permission_displays_the_domain_chain_and_only_then_navigates(billing_client):
    class Hub(FakeBrowser):
        async def open(self,user,url,**kwargs):
            self.calls.append(('open',user,url))
            self.current=snapshot(actions=[{'reference':'bbbbbbbb','label':'Facturación','kind':'link',
                'permission_origin':'https://provider.example'}]) | {'page':ROOT}
            return self.current
        async def observe(self,*args):
            self.calls.append(('observe',*args));return self.current
        async def allow(self,*args,**kwargs):
            self.calls.append(('allow',*args));return self.current
        async def act(self,*args):
            self.calls.append(('act',*args))
            return snapshot(fields=[{'key':'folio','label':'Ticket','required':True,'kind':'input','options':[]}]) | {'page':DEST}
    browser=Hub()
    model=ScriptedModel([[call('portal_browser_observe')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser)
    confirm_profile(telegram,chat);extractor.ticket['billing_url']=ROOT
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'));chat(data=button(telegram,'wa'))
    assert [entry[0] for entry in browser.calls]==['open']
    assert 'hub.example → provider.example' in telegram.messages[-1][1]
    pending=button(telegram,'wa')
    assert app.state.billing.portal_permissions.allowed(owner(app),app.state.billing.store.list('ticket',owner(app))[0])==['https://hub.example']
    chat(data=pending,user=456)
    assert [entry[0] for entry in browser.calls]==['open']
    # El modelo simulado retoma observación y clic luego del consentimiento.
    model.outputs=[[call('portal_browser_observe')]]
    chat(data=pending)
    assert [entry[0] for entry in browser.calls].count('act')==1
    count=len(browser.calls);chat(data=pending)
    assert len(browser.calls)==count and mail.sent==sat.calls==[]


async def test_external_functional_resources_are_not_fetched_or_auto_granted_until_approved():
    asset='https://assets.example/widget.js'
    class Assets(Pages):
        async def fetch(self,url,headers):
            if url==asset:
                self.calls.append((url,headers))
                return 200,{'content-type':'application/javascript'},b'document.body.innerHTML="<label>RFC<input></label>"'
            return await super().fetch(url,headers)
    pages=Assets({ROOT:'<body><script src="'+asset+'"></script>'});manager=BrowserManager(fetcher=pages)
    try:
        first=await manager.open('owner',ROOT,['https://hub.example'],public_route=True)
        assert first['fields']==[] and first['blocked_origins']==['https://assets.example']
        assert [url for url,_ in pages.calls]==[ROOT]
        unchanged=await manager.allow('owner',first['session_id'],['https://hub.example'],public_route=True)
        assert unchanged['fields']==[] and [url for url,_ in pages.calls]==[ROOT]
        approved=await manager.allow('owner',first['session_id'],['https://hub.example','https://assets.example'],public_route=True)
        assert [field['label'] for field in approved['fields']]==['RFC']
        assert [url for url,_ in pages.calls]==[ROOT,ROOT,asset]
    finally:await manager.close()


async def test_existing_twelve_origin_limit_still_stops_an_unapproved_thirteenth_origin():
    allowed=['https://hub.example']+['https://assets'+str(index)+'.example' for index in range(11)]
    pages=Pages({ROOT:'<script src="https://thirteenth.example/library.js"></script>'})
    manager=BrowserManager(fetcher=pages)
    try:
        result=await manager.open('owner',ROOT,allowed,public_route=True)
        session=await manager.get('owner',result['session_id'])
        assert len(session.allowed)==12 and session.network_blocked and session.unsafe_blocked
        assert result['block_reason']=='origin_limit' and [url for url,_ in pages.calls]==[ROOT]
    finally:await manager.close()


def test_checkpoint_from_an_old_broad_grant_cannot_authorize_an_ungranted_resume_host(billing_client):
    class Recorder(FakeBrowser):
        async def open(self,user,url,**kwargs):
            self.allowed=kwargs['allowed'];return await super().open(user,url,**kwargs)
    from .test_billing import ticket
    browser=Recorder();client,app,extractor,*_=billing_client(browser=browser)
    extractor.ticket['billing_url']=ROOT;item=ticket(client)
    permissions=app.state.billing.portal_permissions
    review=permissions.request('owner',item['id'],route=True)
    permissions.decide('owner',review['id'],approve=True)
    item=app.state.billing.resolve_ticket('owner',item['id'])
    item['portal_checkpoint']={'url':DEST,'scope':permissions.scope(item),'expires':time.time()+600,'brand_label':None}
    app.state.billing.store.update('ticket','owner',item['id'],item)
    client.portal.call(app.state.billing.browse_portal,'owner',item['id'])
    assert browser.calls[0][2]==ROOT and browser.allowed==['https://hub.example']
