"""Presupuestos por página/recorrido y reanudación del mismo ticket autorizado."""
import time

import pytest

from backend.app.errors import AppError
from backend.browser_worker.engine import BrowserManager
from .billing_fakes import image
from .fake_model import ScriptedModel,answer,call
from .test_adaptive_browser import FakeBrowser,snapshot
from .test_billing import billing_client
from .test_browser_worker import Pages
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile


ROOT='https://merchant.example/'
ENTRY='https://provider.example/selector?brand=Pizza'


class AssetPages(Pages):
    def __init__(self,count,assets):
        pages={}
        for step in range(count):
            content=''.join('<script src="/assets/'+str(step)+'-'+str(n)+'.js"></script>' for n in range(assets))
            content+=('<a href="/step/'+str(step+1)+'">Facturación</a>' if step<count-1 else '<label>RFC<input></label>')
            pages[ROOT if step==0 else ROOT+'step/'+str(step)]=content
        super().__init__(pages)
    async def fetch(self,url,headers):
        if '/assets/' in url:
            self.calls.append((url,headers))
            return 200,{'content-type':'application/javascript'},b'void 0;'
        return await super().fetch(url,headers)


@pytest.mark.parametrize('public_route',[False,True])
async def test_several_normal_pages_share_route_budget_without_losing_page_limit(public_route):
    pages=AssetPages(3,26)
    manager=BrowserManager(fetcher=pages)
    try:
        current=await manager.open('owner',ROOT,['https://merchant.example'],public_route=public_route)
        for _ in range(2):
            current=await manager.act('owner',current['session_id'],current['observation_id'],current['actions'][0]['reference'])
        if public_route:
            assert not current['network_blocked'] and current['fields'][0]['label']=='RFC'
            assert len(pages.calls)==81 and current['requests_remaining']==159
            assert current['page_requests_remaining']==33
        else:
            assert current['network_blocked'] and current['block_reason']=='request_limit'
            assert len(pages.calls)==60
    finally:
        await manager.close()


async def test_total_route_limit_still_stops_many_pages_even_if_each_page_is_within_budget():
    pages=AssetPages(7,34)
    manager=BrowserManager(fetcher=pages)
    try:
        current=await manager.open('owner',ROOT,['https://merchant.example'],public_route=True)
        for _ in range(6):
            current=await manager.act('owner',current['session_id'],current['observation_id'],current['actions'][0]['reference'])
        assert current['network_blocked'] and current['block_reason']=='request_limit'
        assert current['requests_remaining']==0 and len(pages.calls)==240
    finally:
        await manager.close()


class ExhaustedBrowser(FakeBrowser):
    def blocked(self):
        return snapshot()|{'page':ENTRY.split('?')[0],'bookmark_url':ENTRY,
            'network_blocked':True,'block_reason':'request_limit'}
    async def open(self,owner,url,*,allowed=None,public_route=False):
        self.calls.append(('open',owner,url,allowed))
        assert public_route
        if url==ROOT:
            return self.blocked()
        assert url==ENTRY and allowed==['https://provider.example']
        return snapshot(fields=[{'label':'RFC','key':'rfc','kind':'input','required':True,'options':[],
            'source_url':ENTRY}])|{'page':ENTRY.split('?')[0],'bookmark_url':ENTRY}
    async def observe(self,owner,session):
        self.calls.append(('observe',owner,session))
        return self.blocked()


def received(factory,*,reobserve=False):
    model=ScriptedModel([[call('portal_browser_observe')]]+
        ([[call('portal_browser_observe')]] if reobserve else [])+[[answer('Revisión pausada.')],
        [call('portal_browser_observe')],[answer('Formulario encontrado.')]])
    browser=ExhaustedBrowser()
    values=create(factory,model,browser=browser)
    client,app,extractor,mail,sat,telegram,chat=values
    confirm_profile(telegram,chat)
    extractor.ticket['billing_url']=ROOT
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    chat(data=button(telegram,'wa'))
    return values,browser,model


def test_native_resume_opens_last_observed_page_without_restarting_merchant_or_profile(billing_client):
    values,browser,model=received(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    assert browser.calls[0][2]==ROOT
    assert any(label=='Retomar desde esta página' for label,_ in telegram.messages[-1][2])
    assert app.state.database.usage(owner(app))['runs']==1
    chat(data=button(telegram,'bn'))
    assert [event[0] for event in browser.calls]==['open','close','open']
    assert browser.calls[-1][2]==ENTRY and 'RFC:' in telegram.messages[-1][1]
    assert len(model.inputs)==4 and app.state.database.usage(owner(app))['runs']==2
    assert len(extractor.calls)==2 and mail.sent==sat.calls==[]
    assert len(app.state.billing.store.list('profile',owner(app)))==1
    assert app.state.billing.store.list('portal_memory',owner(app))==[]


def test_model_cannot_restart_exhausted_budget_again_within_same_job(billing_client):
    values,browser,model=received(billing_client,reobserve=True)
    assert [event[0] for event in browser.calls]==['open','observe']
    assert values[1].state.database.usage(owner(values[1]))['runs']==1


def test_expired_review_resumes_checkpoint_with_live_ticket_permission(billing_client):
    values,browser,model=received(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    with app.state.billing.database.connection(True) as connection:
        connection.execute("UPDATE billing_records SET expires=0 WHERE kind='browser_review'")
    chat(data=button(telegram,'bn'))
    assert [event[0] for event in browser.calls]==['open','open']
    assert browser.calls[-1][2]==ENTRY and 'RFC:' in telegram.messages[-1][1]
    assert app.state.database.usage(owner(app))['runs']==2


def test_resume_cannot_bypass_daily_ai_budget(billing_client):
    values,browser,model=received(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    app.state.settings.max_daily_runs=1
    before=list(browser.calls)
    chat(data=button(telegram,'bn'))
    assert browser.calls==before and len(model.inputs)==2
    assert 'límite diario' in telegram.messages[-1][1]


def test_expired_checkpoint_reopens_original_without_reusing_previous_brand(billing_client):
    values,browser,model=received(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    billing=app.state.billing;user=owner(app)
    ticket=billing.store.list('ticket',user)[0]
    ticket['portal_checkpoint']['expires']=0
    billing.store.update('ticket',user,ticket['id'],ticket)
    previous=billing.browser_review(user,ticket['id'])
    previous['brand_label']='Marca de una sesión anterior'
    billing.store.update('browser_review',user,previous['id'],previous)
    async def lost(*args):
        raise AppError(404,'browser_session','Sesión vencida')
    browser.observe=lost
    record=client.portal.call(billing.browse_portal,user,ticket['id'])
    assert browser.calls[-1][2]==ROOT and record['brand_label'] is None


def test_checkpoint_needs_same_owner_ticket_source_permission_and_freshness(billing_client):
    values,browser,model=received(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    billing=app.state.billing
    ticket=billing.store.list('ticket',owner(app))[0]
    assert billing.portal_resume_url(owner(app),ticket)==ENTRY
    assert billing.portal_resume_url('other-owner',ticket) is None
    assert billing.portal_resume_url(owner(app),ticket|{'id':'another-ticket'}) is None
    assert billing.portal_resume_url(owner(app),ticket|{'billing_url':'https://changed.example/'}) is None
    assert billing.portal_resume_url(owner(app),ticket|{'portal_checkpoint':ticket['portal_checkpoint']|{'expires':time.time()-1}}) is None
    assert billing.portal_resume_url(owner(app),ticket|{'portal_checkpoint':ticket['portal_checkpoint']|{'url':ENTRY+'&token=never-reuse'}}) is None
    with billing.database.connection(True) as connection:
        connection.execute("UPDATE billing_records SET expires=0 WHERE kind='portal_grant'")
    assert billing.portal_resume_url(owner(app),ticket) is None
    before=list(browser.calls)
    chat(data=button(telegram,'bn'))
    assert browser.calls==before and '¿Permites revisar este portal?' in telegram.messages[-1][1]
