import json

import pytest
from openai.types.responses import ResponseFunctionWebSearch

from backend.app.billing.extraction import DocumentExtractor,FieldExtraction
from backend.app.billing.models import TicketExtraction
from backend.app.billing.receipt import unrelated_billing_link
from .billing_fakes import TICKET,FISCAL,image
from .fake_model import ScriptedModel,answer,call
from .test_billing import billing_client
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile
from .test_portal_preparation import ScopedExtractor,RealBrowser,ROOT,FORM,preauthorize_fixture_origin


class Modern(ScopedExtractor):
    routing_version=3
    def __init__(self):
        super().__init__();self.searches=[];self.found=ROOT
    async def extract_routing(self,mime,data):
        self.calls.append(('routing',mime))
        return TicketExtraction(**(self.ticket|{'billing_url':'https://survey.example/encuesta?data=private-token',
            'billing_url_evidence':'Tu opinión es importante. Cuéntanos tu experiencia.',
            'billing_email':None,'email_explicitly_for_billing':False}))
    async def discover_official_site(self,merchant):
        self.searches.append(merchant);self.calls.append(('discover',merchant))
        return {'url':self.found,'evidence':'Sitio oficial del establecimiento.'}
    async def extract_field(self,*args):return FieldExtraction(value='175.50')


def modern(factory,extractor=None):
    extractor=extractor or Modern();browser=RealBrowser()
    values=create(factory,ScriptedModel([[call('portal_browser_observe')]]),extractor=extractor,browser=browser)
    client,app,extractor,mail,sat,telegram,chat=values
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('green');chat(document='receipt')
    pending=app.state.billing.store.list('ticket',owner(app))[0]
    preauthorize_fixture_origin(app,pending|{'billing_url':ROOT},'https://provider.example')
    return values,browser


def test_survey_qr_is_discarded_then_official_homepage_is_searched_and_followed_with_one_consent(billing_client):
    values,browser=modern(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    consent=button(telegram,'an')
    assert extractor.searches==browser.calls==[]
    chat(data=consent,user=456)
    assert extractor.searches==[]
    chat(data=consent)
    item=app.state.billing.store.list('ticket',owner(app))[0]
    assert extractor.searches==[TICKET['merchant']]
    assert item['billing_url']==ROOT and item['portal_discovery']['source']=='web_search'
    assert [c[0] for c in extractor.calls]==['csf','routing','discover','fields']
    assert browser.calls==['open','act']
    assert 'Formulario encontrado' in telegram.messages[-1][1]
    assert 'Importe del ticket: 116.00' in telegram.messages[-1][1]
    assert not any('private-token' in text or 'Importe: Pendiente' in text for _,text,_ in telegram.messages)
    assert all(secret not in json.dumps(app.state.service.agent.model.inputs) for secret in [FISCAL['rfc'],FISCAL['name']])
    assert mail.sent==sat.calls==[] and not item.get('delivery')
    assert app.state.billing.portal_permissions.route_allowed(owner(app),item)


def test_basic_visible_values_are_read_in_the_first_real_sdk_call(billing_client):
    data={key:TICKET.get(key) for key in ('is_ticket','readable','merchant','billing_url','billing_url_evidence',
        'billing_email','billing_email_evidence','email_explicitly_for_billing','total','folio','purchase_date','issuer_rfc','currency')}
    model=ScriptedModel([[answer(json.dumps(data))]])
    client,app,*_=billing_client(model=model)
    extractor=DocumentExtractor(app.state.service,app.state.settings)
    result=client.portal.call(extractor.extract_routing,'image/jpeg',image())
    assert result.total=='116.00' and result.folio=='T-1234' and result.purchase_date=='2026-10-24'
    assert len(model.inputs)==1


@pytest.mark.parametrize('search_performed,selected,cited,ambiguous,expected',[
    (True,ROOT,ROOT,False,ROOT),
    (False,ROOT,ROOT,False,None),
    (True,ROOT,'https://directory.example/',False,None),
    (True,'https://www.merchant.example/',ROOT,False,'https://www.merchant.example/'),
    (True,ROOT,'https://other.merchant.example/',False,None),
    (True,ROOT,ROOT,True,None),
    (True,'https://survey.example/encuesta', 'https://survey.example/encuesta',False,None),
])
def test_official_lookup_requires_actual_search_sources_and_never_accepts_an_invented_host(billing_client,search_performed,selected,cited,ambiguous,expected):
    outputs=[]
    if search_performed:
        outputs.append(ResponseFunctionWebSearch(id='ws_demo',type='web_search_call',status='completed',
            action={'type':'search','query':'Restaurante ejemplo sitio oficial','sources':[{'type':'url','url':cited}]}))
    outputs.append(answer(json.dumps({'url':selected,'evidence':'Página oficial.','ambiguous':ambiguous})))
    class SearchModel(ScriptedModel):
        async def get_response(self,*args,**kwargs):
            self.requested_tools=args[3] if len(args)>3 else kwargs.get('tools')
            self.requested_settings=args[2] if len(args)>2 else kwargs.get('model_settings')
            return await super().get_response(*args,**kwargs)
    model=SearchModel([outputs]);client,app,*_=billing_client(model=model)
    extractor=DocumentExtractor(app.state.service,app.state.settings)
    result=client.portal.call(extractor.discover_official_site,'Restaurante ejemplo')
    assert result['url']==expected
    assert model.requested_settings.tool_choice=='required'
    assert 'Restaurante ejemplo' in str(model.inputs) and FISCAL['rfc'] not in str(model.inputs)


def test_unread_amount_is_requested_as_plain_text_without_rereading_csf_or_ticket(billing_client):
    class Missing(Modern):
        async def extract_fields(self,*args):return (await super().extract_fields(*args)).model_copy(update={'total':None})
    extractor=Missing();extractor.ticket['total']=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    assert 'No leí el importe' in telegram.messages[-1][1]
    data=next(data for label,data in telegram.messages[-1][2] if label=='Completar importe')
    before=len(extractor.calls);chat(data=data);chat('175.50')
    item=app.state.billing.store.list('ticket',owner(app))[0]
    assert item['total']=='175.50' and len(extractor.calls)==before
    assert len(app.state.billing.store.list('profile',owner(app)))==1
    assert len(app.state.billing.store.list('ticket',owner(app)))==1


def test_no_verified_official_site_asks_for_manual_url_and_never_chooses_a_survey(billing_client):
    extractor=Modern();extractor.found=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    assert browser.calls==[] and 'web oficial verificable' in telegram.messages[-1][1]
    chat(data=button(telegram,'dw'));chat(ROOT)
    assert browser.calls==['open','act'] and len(extractor.searches)==1
    assert 'Formulario encontrado' in telegram.messages[-1][1]
    assert mail.sent==sat.calls==[]


def test_amount_crop_is_analyzed_only_for_the_requested_field_and_stays_in_the_same_request(billing_client):
    extractor=Modern();extractor.ticket['total']=None;extractor.found=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    item=app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='td:'+item['id']+':total');telegram.files['crop']=image('blue');chat(document='crop')
    assert len(app.state.billing.store.list('ticket',owner(app)))==1
    chat(data=button(telegram,'la'));chat(data=button(telegram,'lu'))
    assert app.state.billing.resolve_ticket(owner(app),item['id'])['total']=='175.50'
    assert app.state.telegram.workflow.active(owner(app))['id']==item['id']


@pytest.mark.parametrize('address,evidence,wrong',[
    ('https://ecube.example/encuesta/a?data=token','Escanea para facturar',True),
    ('https://survey.example/','Tu opinión es importante',True),
    ('https://experiencias.example.com/facturacion','Facturación electrónica',False),
    ('https://merchant.example/','Factura en nuestra web',False),
])
def test_billing_purpose_is_not_guessed_from_any_qr_or_from_a_brand_word(address,evidence,wrong):
    assert unrelated_billing_link(address,evidence) is wrong


def test_printed_official_website_is_followed_without_paying_for_a_web_search(billing_client):
    extractor=Modern()
    extractor.ticket.update(merchant_website=ROOT,merchant_website_evidence='Visita nuestra página oficial: '+ROOT)
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    assert extractor.searches==[] and browser.calls==['open','act']
    item=app.state.billing.store.list('ticket',owner(app))[0]
    assert item['portal_discovery']['source']=='printed'


def test_a_portal_requesting_only_known_basic_values_does_not_reread_the_ticket(billing_client):
    values,browser=modern(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    browser.pages.pages[FORM]='''<form><label>RFC<input required></label><label>Ticket<input required></label><label>Fecha<input type=date required></label></form>'''
    chat(data=button(telegram,'an'))
    assert [c[0] for c in extractor.calls]==['csf','routing','discover']
    assert 'Importe del ticket: 116.00' in telegram.messages[-1][1]
    assert 'T-1234' in telegram.messages[-1][1]


def test_old_survey_selection_is_repaired_without_rereading_profile_or_exposing_query(billing_client):
    from .test_telegram_workflow import first_ticket
    values,identifier=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    item=app.state.billing.resolve_ticket(owner(app),identifier)
    app.state.billing.store.update('ticket',owner(app),identifier,item|{'billing_url':'https://survey.example/encuesta?data=private-token',
        'billing_url_evidence':'Tu opinión es importante','billing_email':None,'email_explicitly_for_billing':False,
        'total':None,'portal_stage':'awaiting_form'})
    before=len(extractor.calls);chat('/estado')
    repaired=app.state.billing.resolve_ticket(owner(app),identifier)
    assert repaired['billing_url'] is None and len(extractor.calls)==before
    assert 'private-token' not in telegram.messages[-1][1] and 'Importe: Pendiente' not in telegram.messages[-1][1]
    assert 'No leí: importe' in telegram.messages[-1][1]
    assert button(telegram,'df') and button(telegram,'dw')
    assert len(app.state.billing.store.list('profile',owner(app)))==1


def test_actual_submission_waits_for_missing_amount_even_when_the_portal_does_not_request_it(billing_client):
    from .test_portal_submission import enable_transactions,PortalPages
    class Missing(Modern):
        async def extract_fields(self,*args):return (await super().extract_fields(*args)).model_copy(update={'total':None})
    extractor=Missing();extractor.ticket['total']=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'));pages=PortalPages();enable_transactions(browser,pages)
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='lv:'+ticket['id'])
    assert 'importe del ticket' in telegram.messages[-1][1]
    assert not any(data.startswith('ls:') for _,data in telegram.messages[-1][2])
    chat('175.50')
    assert 'Importe del ticket: 175.50' in telegram.messages[-1][1]
    assert button(telegram,'ls') and pages.sent==mail.sent==sat.calls==[]


def test_an_officially_linked_provider_opened_by_web_search_counts_as_a_real_source(billing_client):
    provider='https://provider.example/form'
    model=ScriptedModel([[
        ResponseFunctionWebSearch(id='ws_open',type='web_search_call',status='completed',action={'type':'open_page','url':provider}),
        answer(json.dumps({'url':provider,'evidence':'Proveedor enlazado por el sitio oficial del comercio.','ambiguous':False}))]])
    client,app,*_=billing_client(model=model)
    extractor=DocumentExtractor(app.state.service,app.state.settings)
    result=client.portal.call(extractor.discover_official_site,'Restaurante ejemplo')
    assert result['url']==provider


def test_expired_official_cache_does_not_block_a_new_lookup_for_the_same_business(billing_client):
    values,browser=modern(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    cached=app.state.billing.store.list('portal_discovery',owner(app))[0]
    with app.state.database.connection(True) as conn:
        conn.execute('UPDATE billing_records SET expires=0 WHERE id=?',(cached['id'],))
    app.state.billing.store.update('ticket',owner(app),ticket['id'],ticket|{'billing_url':None})
    result=client.portal.call(app.state.billing.portal_discovery.find,owner(app),ticket['id'])
    assert result['billing_url']==ROOT and len(extractor.searches)==2
    assert len(app.state.billing.store.list('portal_discovery',owner(app)))==1


def test_known_first_read_values_do_not_need_the_expired_original_photo(billing_client):
    values,browser=modern(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    browser.pages.pages[FORM]='''<form><label>RFC<input required></label><label>Ticket<input required></label><label>Fecha<input type=date required></label></form>'''
    chat(data=button(telegram,'an'))
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    assert not ticket.get('portal_data_document_id')
    with app.state.database.connection(True) as conn:
        conn.execute('UPDATE billing_records SET expires=0 WHERE id=?',(ticket['document_id'],))
    calls=len(extractor.calls)
    chat(data='bn:'+ticket['id'])
    assert 'Reenvía' not in telegram.messages[-1][1]
    assert app.state.billing.resolve_ticket(owner(app),ticket['id']).get('portal_data_error') is None
    assert len(extractor.calls)==calls
