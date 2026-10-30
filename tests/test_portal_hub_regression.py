"""Regresión integrada: dos direcciones, hub con marcas y formulario en otro dominio."""
from backend.app.billing.models import TicketExtraction
from backend.app.billing.portal_inspection import field_key
from .billing_fakes import TICKET,image
from .fake_model import ScriptedModel,call
from .test_billing import billing_client
from .test_portal_preparation import RealBrowser,ScopedExtractor
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile


HUB='https://hub.example.com.mx/'
DEST='https://provider.example/request'
BAD='https://branch.invalid/'
FORM='<form method=post action=/never-send><label>RFC<input required></label><label>Ticket<input required maxlength=9></label><label>Tienda<input required maxlength=5></label><label>Fecha dd/mm/aaaa<input required></label></form>'


class PublicBrowser(RealBrowser):
    async def allow(self,user,session,allowed,**kwargs):
        self.calls.append('allow')
        return self.keys(await self.manager.allow(user,session,allowed,**kwargs))


def test_printed_hub_then_matching_brand_stops_for_domain_consent_and_reads_only_observed_fields(billing_client):
    class Receipt(ScopedExtractor):
        async def extract_routing(self,mime,data):
            self.calls.append(('routing',mime))
            return TicketExtraction(**(TICKET|{'merchant':'Cocina Aurora','folio':'123456789','billing_email':None,
                'email_explicitly_for_billing':False,'billing_url':BAD,
                'printed_addresses':[{'url':HUB,'evidence':'Tu documento obténlo en el portal: '+HUB,'purpose':'billing'},
                    {'url':BAD,'evidence':'Datos Sucursal: '+BAD,'purpose':'other'}]}))
        async def extract_fields(self,mime,data,fields):
            self.calls.append(('fields',[{'key':item['key'],'label':item['label']} for item in fields]))
            return TicketExtraction(**(TICKET|{'merchant':'Cocina Aurora','folio':'123456789','store_number':'00123'}))
    browser=PublicBrowser()
    browser.pages.pages={HUB:'<h1>Elige tu establecimiento</h1><a href="'+DEST+'"><img alt="Cocina Aurora" src="/a.png"></a><a href="/other"><img alt="Otra cocina" src="/b.png"></a>',DEST:FORM}
    checked=[]
    async def probe(url):
        checked.append(url);return {'url':url,'reachable':True,'reason':None,'status':200}
    model=ScriptedModel([[call('portal_browser_observe')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser,extractor=Receipt(),portal_probe=probe)
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'));chat(data=button(telegram,'wa'))
    assert checked==[HUB] and [url for url,_ in browser.pages.calls]==[HUB]
    assert 'hub.example.com.mx → provider.example' in telegram.messages[-1][1]
    assert not any(event[0]=='fields' for event in extractor.calls)
    model.outputs=[[call('portal_browser_observe')]]
    chat(data=button(telegram,'wa'))
    item=app.state.billing.store.list('ticket',owner(app))[0]
    observed=app.state.billing.browser_review(owner(app),item['id'])['snapshot']
    assert 'Formulario encontrado' in telegram.messages[-1][1]
    assert {field['key'] for field in observed['fields']}=={'rfc','folio','store_number','purchase_date'}
    assert {field['key']:field['max_length'] for field in observed['fields']}['folio']==9
    assert {field['key']:field['max_length'] for field in observed['fields']}['store_number']==5
    assert {field['key'] for field in extractor.calls[-1][1]}=={'folio','store_number','purchase_date'}
    assert item['folio']=='123456789' and item['store_number']=='00123'
    assert item['portal_rejected_addresses'][0]['url']==BAD and item['portal_rejected_addresses'][0]['reason']=='printed_other'
    session=browser.manager.sessions[observed['session_id']]
    assert client.portal.call(session.page.locator('input').evaluate_all,'nodes=>nodes.map(e=>e.value)')==['','','','']
    assert 'fill' not in browser.calls and mail.sent==sat.calls==[] and not item.get('delivery')
