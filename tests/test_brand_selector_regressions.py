"""Selector de marcas con peticiones auxiliares denegadas y enlaces de pie."""
import json

import pytest

from backend.app.billing.browser import Observation
from backend.app.billing.portal_navigation import next_public_action
from backend.app.billing.portal_inspection import field_key
from backend.browser_worker.engine import BrowserManager
from .billing_fakes import FISCAL,image
from .fake_model import ScriptedModel,answer,call
from .test_adaptive_browser import FakeBrowser,snapshot
from .test_billing import billing_client
from .test_browser_worker import Pages
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile


@pytest.mark.parametrize('phrase',['Selecciona la marca','Elige una de estas marcas','Seleccione su restaurante','Select a brand'])
@pytest.mark.parametrize('script',[
    "const r=new XMLHttpRequest();r.open('POST','/public-init');r.send('not-forwarded');",
    "fetch('/public-init',{method:'POST',body:'not-forwarded'}).catch(()=>{});",
    "navigator.sendBeacon('/public-init','not-forwarded');",
])
async def test_auxiliary_post_does_not_freeze_brand_link_or_choose_generic_footer(phrase,script):
    root='https://group.example/brands'
    form='https://provider.example/form?opc=Pizza'
    pages=Pages({root:'''<title>Facturación</title><body><h2>'''+phrase+'''</h2>
        <a href="'''+form+'''"><img src="/logo-pizza-alfa.png"></a>
        <a href="https://provider.example/form?opc=Coffee"><img src="/logo-cafe-beta.png"></a>
        <footer><a href="https://group.example/invoices">Facturación Electrónica</a></footer>
        <script>'''+script+'''</script>
        ''',form:'<label>RFC<input name="rfc"></label>'})
    manager=BrowserManager(fetcher=pages)
    try:
        observed=await manager.open('owner',root,['https://group.example'],public_route=True)
        assert not observed['network_blocked'] and observed['omitted_writes']==1
        assert observed['block_reason'] is None and observed['brand_selection']
        assert [url for url,_ in pages.calls]==[root]
        # El cliente agrega los conceptos de campos; aquí no hay campos aún.
        Observation.model_validate(observed)
        selected=next_public_action({'merchant':'Pizza Alfa'},observed)
        assert selected['label']=='logo pizza alfa'
        current=await manager.act('owner',observed['session_id'],observed['observation_id'],selected['reference'])
        assert current['fields'][0]['label']=='RFC'
        assert [url for url,_ in pages.calls]==[root,form]
        assert not current['submitted'] and 'not-forwarded' not in json.dumps(current)
    finally:
        await manager.close()


def test_explicit_brand_selector_with_ambiguous_merchant_does_not_return_to_footer():
    observed=snapshot(actions=[
        {'reference':'aaaaaaaa','label':'Pizza Alfa','kind':'link','brand':True},
        {'reference':'bbbbbbbb','label':'Café Beta','kind':'link','brand':True},
        {'reference':'cccccccc','label':'Facturación Electrónica','kind':'link'}])
    observed['brand_selection']=True
    assert next_public_action({'merchant':'Operadora del Grupo'},observed) is None
    assert next_public_action({'merchant':'Pizza Alfa'},observed)['reference']=='aaaaaaaa'


def test_explicit_brand_selector_without_readable_options_does_not_choose_footer():
    observed=snapshot(actions=[
        {'reference':'cccccccc','label':'- Facturación Electrónica','kind':'link'}])
    observed['brand_selection']=True
    assert next_public_action({'merchant':'Pizza Alfa'},observed) is None


def test_explicit_brand_selector_can_match_a_single_observed_option():
    observed=snapshot(actions=[{'reference':'aaaaaaaa','label':'Pizza Alfa','kind':'link','brand':True},
        {'reference':'cccccccc','label':'Facturación Electrónica','kind':'link'}])
    observed['brand_selection']=True
    assert next_public_action({'merchant':'Pizza Alfa'},observed)['reference']=='aaaaaaaa'


async def test_hidden_brand_controls_do_not_send_public_route_to_corporate_footer():
    root='https://provider.example/brands'
    pages=Pages({root:'''<h2>Selecciona la marca</h2>
        <section hidden><a href="/form?brand=Pizza"><img alt="Pizza Alfa"></a>
        <a href="/form?brand=Coffee"><img alt="Café Beta"></a></section>
        <footer><a href="https://group.example/invoices">Facturación Electrónica</a></footer>'''})
    manager=BrowserManager(fetcher=pages)
    try:
        observed=await manager.open('owner',root,['https://provider.example'],public_route=True)
        assert observed['brand_selection'] and len(observed['actions'])==1
        assert next_public_action({'merchant':'Pizza Alfa'},observed) is None
        assert [url for url,_ in pages.calls]==[root]
    finally:
        await manager.close()


def test_model_cannot_follow_footer_in_explicit_brand_selector_without_options(billing_client):
    class MissingBrands(FakeBrowser):
        async def open(self,*args,**kwargs):
            result=await super().open(*args,**kwargs)
            result['brand_selection']=True
            result['actions']=[{'reference':'bbbbbbbb','label':'Facturación Electrónica','kind':'link'}]
            return result
    browser=MissingBrands()
    model=ScriptedModel([[call('portal_browser_observe')],
        [call('portal_browser_navigate',observation_id='aaaaaaaa',reference='bbbbbbbb')],
        [answer('Faltan opciones de marca.')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser)
    confirm_profile(telegram,chat)
    extractor.ticket.update(merchant='Pizza Alfa',billing_url='https://merchant.example/')
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    chat(data=button(telegram,'wa'))
    assert [item[0] for item in browser.calls]==['open']
    assert 'brand_selection_pending' in str(model.inputs[-1])
    assert 'no se detectaron sus opciones' in telegram.messages[-1][1]
    assert mail.sent==sat.calls==[]


def test_telegram_sdk_reaches_brand_form_despite_startup_post_and_footer_link(billing_client):
    root='https://group.example/brands'
    form='https://provider.example/form?opc=Pizza'
    pages=Pages({root:'''<h2>Selecciona la marca</h2>
        <a href="'''+form+'''"><img src="logo-pizza-alfa.png"></a>
        <a href="https://provider.example/form?opc=Coffee"><img src="logo-cafe-beta.png"></a>
        <a href="https://group.example/invoices">Facturación Electrónica</a>
        <script>fetch('/init',{method:'POST',body:'never-forward'}).catch(()=>{});</script>''',
        form:'<label>RFC<input></label>'})
    class RealPublicBrowser(FakeBrowser):
        def __init__(self):
            super().__init__()
            self.manager=BrowserManager(fetcher=pages)
        def metadata(self,result):
            for item in result['fields']:
                item['key']=field_key(item['label'])
            return result
        async def open(self,*args,**kwargs):
            self.calls.append('open')
            return self.metadata(await self.manager.open(args[0],args[1],kwargs['allowed'],public_route=kwargs.get('public_route',False)))
        async def act(self,*args):
            self.calls.append('act')
            return self.metadata(await self.manager.act(*args))
        async def close(self):
            await self.manager.close()
    browser=RealPublicBrowser()
    model=ScriptedModel([[call('portal_browser_observe')],[answer('Formulario encontrado.')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser)
    confirm_profile(telegram,chat)
    extractor.ticket.update(merchant='Pizza Alfa',billing_url=root)
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    before=len(telegram.messages)
    chat(data=button(telegram,'wa'))
    assert browser.calls==['open','act'] and [url for url,_ in pages.calls]==[root,form]
    assert len(telegram.messages)==before+1 and len(model.inputs)==2
    assert 'RFC: '+FISCAL['rfc'] in telegram.messages[-1][1]
    assert 'Guardar portal' in str(telegram.messages[-1][2])
    assert FISCAL['rfc'] not in json.dumps(model.inputs)
    assert app.state.database.usage(owner(app))['runs']==1 and mail.sent==sat.calls==[]


async def test_auxiliary_write_retry_storm_still_hits_page_request_limit():
    root='https://merchant.example/'
    pages=Pages({root:'''<script>
        Promise.all(Array.from({length:65},()=>fetch('/init',{method:'POST'}).catch(()=>{})));
        </script><a href="/form">Facturación</a>'''})
    manager=BrowserManager(fetcher=pages)
    try:
        observed=await manager.open('owner',root,['https://merchant.example'],public_route=True)
        assert observed['network_blocked'] and observed['block_reason']=='page_request_limit'
        assert observed['omitted_writes']>=60 and [url for url,_ in pages.calls]==[root]
    finally:
        await manager.close()


async def test_main_form_submission_is_still_denied_and_reports_method_instead_of_limit():
    root='https://merchant.example/'
    pages=Pages({root:'''<form method="post" action="/issue" id="invoice"></form>
        <script>setTimeout(()=>document.getElementById('invoice').submit(),100)</script>'''})
    manager=BrowserManager(fetcher=pages)
    try:
        observed=await manager.open('owner',root,['https://merchant.example'],public_route=True)
        await manager.sessions[observed['session_id']].page.wait_for_timeout(250)
        current=await manager.observe('owner',observed['session_id'])
        assert current['network_blocked'] and current['block_reason']=='unsupported_request'
        assert [url for url,_ in pages.calls]==[root] and not current['submitted']
    finally:
        await manager.close()
