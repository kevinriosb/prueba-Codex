import json
import time
from functools import partial

import pytest

from backend.app.billing.models import TicketExtraction
from backend.app.billing.portal_inspection import field_key
from backend.app.errors import AppError
from backend.browser_worker.engine import BrowserManager
from backend.browser_worker.policy import BrowserError
from .billing_fakes import FakeExtractor,FISCAL,TICKET,image
from .fake_model import ScriptedModel,call
from .test_billing import billing_client
from .test_browser_worker import Pages
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile


ROOT='https://merchant.example/'
FORM='https://provider.example/form?brand=Pizza'
HTML='''<form action="/issue" method="post"><label>RFC<input required></label><label>Ticket<input required></label>
    <label>Tienda<input required></label><label>Fecha dd/mm/aaaa<input required></label><button>Solicitar factura</button></form>
    <script>document.addEventListener('input',e=>fetch('/leak?value='+e.target.value).catch(()=>{}))</script>'''


class ScopedExtractor(FakeExtractor):
    async def extract_routing(self,mime,data):
        self.calls.append(('routing',mime))
        values={key:None for key in TicketExtraction.model_fields}
        values.update(is_ticket=True,readable=True,merchant='Pizza Alfa',billing_url=ROOT,
            email_explicitly_for_billing=False)
        return TicketExtraction.model_validate(values)
    async def extract_fields(self,mime,data,fields):
        self.calls.append(('fields',[{'key':f['key'],'label':f['label']} for f in fields]))
        return TicketExtraction(**(TICKET|{'merchant':'Pizza Alfa','store_number':'07001'}))


class RealBrowser:
    enabled=True
    def __init__(self):
        self.pages=Pages({ROOT:'<a href="'+FORM+'">Facturación</a>',FORM:HTML})
        self.manager=BrowserManager(fetcher=self.pages)
        self.calls=[]
    def keys(self,snapshot):
        for item in snapshot['fields']:item['key']=field_key(item['label'])
        return snapshot
    async def open(self,owner,url,**kwargs):
        self.calls.append('open')
        return self.keys(await self.manager.open(owner,url,kwargs['allowed'],public_route=kwargs.get('public_route',False)))
    async def observe(self,owner,identifier):
        self.calls.append('observe')
        try:return self.keys(await self.manager.observe(owner,identifier))
        except BrowserError as error:raise AppError(error.status,error.code,error.message) from None
    async def act(self,*args):
        self.calls.append('act')
        return self.keys(await self.manager.act(*args))
    async def close_session(self,*args):return await self.manager.close_session(*args)
    async def fill(self,*args):
        self.calls.append('fill')
        return await self.manager.fill(*args)
    async def filled(self,*args):return await self.manager.filled(*args)
    async def close(self):await self.manager.close()


def prepared(factory,*,extractor=None,before_portal=None):
    extractor=extractor or ScopedExtractor();browser=RealBrowser()
    model=ScriptedModel([[call('portal_browser_observe')]])
    values=create(factory,model,browser=browser,extractor=extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    assert ticket.get('portal_stage')=='awaiting_form' and ticket['store_number'] is None
    assert not any(label.startswith('Completar ') for label,_ in telegram.messages[-1][2])
    if before_portal:before_portal(app,ticket)
    chat(data=button(telegram,'wa'))
    return values,browser,model


def test_portal_is_observed_before_directed_extraction_and_filling_needs_the_native_button(billing_client):
    values,browser,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    assert [item[0] for item in extractor.calls]==['csf','routing','fields']
    requested=extractor.calls[-1][1]
    assert {f['key'] for f in requested}=={'folio','store_number','purchase_date'}
    assert all(f['key'] not in {'rfc','name','email'} for f in requested)
    assert browser.calls==['open','act'] and 'fill' not in browser.calls
    assert '24/10/2026' in telegram.messages[-1][1] and '07001' in telegram.messages[-1][1]
    assert 'xg:' in str(telegram.messages[-1][2]) and len(model.inputs)==1
    assert FISCAL['rfc'] not in json.dumps(model.inputs)
    calls=list(browser.pages.calls)
    chat(data=button(telegram,'xg'))
    assert browser.calls[-1]=='fill' and browser.pages.calls==calls
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    assert not ticket['portal_fill']['submitted'] and not ticket.get('delivery')
    session=browser.manager.sessions[ticket['portal_fill']['session_id']]
    # Inspección local de la prueba: el endpoint público no expone estos valores.
    actual=client.portal.call(session.page.locator('input').evaluate_all,'nodes=>nodes.map(e=>e.value)')
    assert actual==[FISCAL['rfc'],TICKET['folio'],'07001','24/10/2026']
    assert 'No se envió' in telegram.messages[-1][1] and mail.sent==sat.calls==[]
    assert len(model.inputs)==1 and len(extractor.calls)==3


def test_an_approval_cannot_be_replayed_or_used_by_another_user(billing_client):
    values,browser,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    data=button(telegram,'xg')
    before=list(browser.calls)
    chat(data=data,user=456)
    assert browser.calls==before
    chat(data=data)
    assert browser.calls.count('fill')==1
    chat(data=data)
    assert browser.calls.count('fill')==1 and mail.sent==sat.calls==[]


def test_changed_purchase_values_require_another_review_before_filling(billing_client):
    values,browser,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    data=button(telegram,'xg')
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    ticket['store_number']='99999'
    app.state.billing.store.update('ticket',owner(app),ticket['id'],ticket)
    chat(data=data)
    assert 'fill' not in browser.calls and 'Cambió' in telegram.messages[-1][1]


def test_reopening_a_form_reuses_its_directed_reading_without_more_ai(billing_client):
    values,browser,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    count=len(extractor.calls)
    chat(data='bn:'+ticket['id'])
    assert len(extractor.calls)==count and len(model.inputs)==1
    document=app.state.billing.document(owner(app),ticket['document_id'])
    assert document['_expires']-time.time()>6*86400


def test_missing_store_asks_only_for_that_field_and_does_not_repeat_the_same_reading(billing_client):
    class Partial(ScopedExtractor):
        async def extract_fields(self,*args):
            return (await super().extract_fields(*args)).model_copy(update={'store_number':None})
    values,browser,_=prepared(billing_client,extractor=Partial())
    client,app,extractor,mail,sat,telegram,chat=values
    buttons=telegram.messages[-1][2]
    assert any(label=='Completar Tienda' for label,_ in buttons)
    assert not any(data.startswith(('xg:','xa:')) for _,data in buttons)
    assert 'Falta: Tienda' in telegram.messages[-1][1]
    count=len(extractor.calls)
    chat(data=button(telegram,'td'))
    chat('/dato_ticket 07001')
    assert len(extractor.calls)==count and 'Llenar formulario' in str(telegram.messages[-1][2])
    assert len(app.state.billing.store.list('profile',owner(app)))==1


def test_older_analysis_requires_specific_native_approval_for_the_new_reading(billing_client):
    def legacy(app,ticket):
        document=app.state.billing.document(owner(app),ticket['document_id'])
        document.pop('analysis_scope')
        app.state.billing.store.update('document',owner(app),document['id'],document)
    values,browser,_=prepared(billing_client,before_portal=legacy)
    client,app,extractor,mail,sat,telegram,chat=values
    assert [item[0] for item in extractor.calls]==['csf','routing']
    approval=button(telegram,'xa')
    chat(data=approval,user=456)
    assert len(extractor.calls)==2
    chat(data=approval)
    assert len(extractor.calls)==3 and 'xg:' in str(telegram.messages[-1][2])
    chat(data=approval)
    assert len(extractor.calls)==3


def test_expired_photo_is_replaced_on_the_same_ticket_without_requesting_a_csf(billing_client):
    def expired(app,ticket):
        with app.state.database.connection(True) as conn:
            conn.execute('UPDATE billing_records SET expires=? WHERE id=?',(time.time()-1,ticket['document_id']))
    values,browser,_=prepared(billing_client,before_portal=expired)
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    profile=ticket['profile_id'];identifier=ticket['id']
    assert len(extractor.calls)==2 and 'foto temporal venció' in telegram.messages[-1][1]
    chat(data=button(telegram,'xu'))
    telegram.files['again']=image('red');chat(document='again')
    assert len(extractor.calls)==2
    chat(data=button(telegram,'xa'))
    tickets=app.state.billing.store.list('ticket',owner(app))
    assert len(tickets)==1 and tickets[0]['id']==identifier and tickets[0]['profile_id']==profile
    assert len(extractor.calls)==3 and 'xg:' in str(telegram.messages[-1][2])
    assert len(app.state.billing.store.list('profile',owner(app)))==1


def test_replacement_photo_from_another_purchase_cannot_change_the_saved_ticket(billing_client):
    values,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    async def other(*args):return TicketExtraction(**(TICKET|{'folio':'DIFFERENT','store_number':'09009'}))
    extractor.extract_fields=other
    app.state.billing.portal_preparation.replace_source(owner(app),ticket['id'],image('blue'))
    record=app.state.billing.browser_review(owner(app),ticket['id'])
    with pytest.raises(AppError) as error:
        client.portal.call(partial(app.state.billing.portal_preparation.read_fields,owner(app),ticket['id'],record['snapshot'],consent=True))
    assert error.value.code=='portal_source_mismatch'
    saved=app.state.billing.resolve_ticket(owner(app),ticket['id'])
    assert saved['folio']==TICKET['folio'] and saved['store_number']=='07001'


@pytest.mark.parametrize('change',['form','profile'])
def test_changed_form_or_fiscal_value_requires_a_new_fill_review(billing_client,change):
    values,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    approval=button(telegram,'xg')
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    if change=='profile':
        profile=app.state.billing.store.get('profile',owner(app),ticket['profile_id'])
        app.state.billing.store.update('profile',owner(app),profile['id'],profile|{'rfc':'BBB010101BBB'})
    else:
        record=app.state.billing.browser_review(owner(app),ticket['id'])
        page=browser.manager.sessions[record['snapshot']['session_id']].page
        client.portal.call(page.locator('input').nth(2).evaluate,"e=>e.maxLength=8")
    chat(data=approval)
    assert 'fill' not in browser.calls and 'Cambió' in telegram.messages[-1][1]


def test_lost_fill_response_can_be_checked_without_replaying_or_returning_private_values(billing_client):
    values,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    original=browser.fill
    async def uncertain(*args):
        await original(*args)
        raise AppError(504,'browser_timeout','No se confirmó la respuesta.')
    browser.fill=uncertain
    chat(data=button(telegram,'xg'))
    assert 'No se confirmó' in telegram.messages[-1][1]
    chat(data=button(telegram,'xs'))
    assert 'sesión activa' in telegram.messages[-1][1] and browser.calls.count('fill')==1
    assert app.state.billing.store.list('portal_fill_review',owner(app))[0]['_state']=='complete'
    assert mail.sent==sat.calls==[]


def test_directed_reading_shares_daily_quota_and_a_failure_is_not_silently_retried(billing_client):
    def full(app,ticket):
        app.state.billing.settings.billing_max_daily_extractions=2
    values,browser,_=prepared(billing_client,before_portal=full)
    client,app,extractor,mail,sat,telegram,chat=values
    assert len(extractor.calls)==2 and 'límite diario' in telegram.messages[-1][1]
    assert 'xg:' not in str(telegram.messages[-1][2])
    class Failing(ScopedExtractor):
        async def extract_fields(self,*args):
            self.calls.append(('fields',[]))
            raise AppError(502,'extraction_provider','Proveedor no disponible.')
    values,browser,_=prepared(billing_client,extractor=Failing())
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    assert len(extractor.calls)==3 and 'Proveedor no disponible' in telegram.messages[-1][1]
    assert 'xa:' not in str(telegram.messages[-1][2])
    chat(data='bn:'+ticket['id'])
    assert len(extractor.calls)==3


def test_native_correction_invalidates_old_fill_and_survives_another_directed_read(billing_client):
    values,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    approval=button(telegram,'xg')
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='xe:'+ticket['id']+':store_number')
    chat('/dato_ticket 00042')
    chat(data=approval)
    assert 'fill' not in browser.calls
    record=app.state.billing.browser_review(owner(app),ticket['id'])
    snapshot=json.loads(json.dumps(record['snapshot']))
    snapshot['fields'][2]['label']='Número de Tienda'
    client.portal.call(partial(app.state.billing.portal_preparation.read_fields,owner(app),ticket['id'],snapshot,consent=True))
    assert app.state.billing.resolve_ticket(owner(app),ticket['id'])['store_number']=='00042'


def test_ticket_retention_is_bounded_and_duplicate_upload_does_not_renew_it(billing_client):
    from pydantic import ValidationError
    from backend.app.config import Settings
    values,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    records=app.state.billing.store.list('document',owner(app))
    csf=next(d for d in records if d['purpose']=='csf')
    ticket=next(d for d in records if d['purpose']=='ticket')
    assert 0<csf['_expires']-time.time()<=86400
    assert 6*86400<ticket['_expires']-time.time()<=7*86400
    chat(document='receipt')
    assert app.state.billing.document(owner(app),ticket['id'])['_expires']==ticket['_expires']
    assert len(extractor.calls)==3
    for value in (0,169):
        with pytest.raises(ValidationError):Settings(billing_ticket_retention_hours=value)


@pytest.mark.parametrize('kind',['partial','unknown','long','checkbox'])
def test_incomplete_or_incompatible_required_fields_never_offer_fill(billing_client,kind):
    values,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    record=app.state.billing.browser_review(owner(app),ticket['id'])
    snapshot=json.loads(json.dumps(record['snapshot']))
    if kind=='partial':snapshot['partial']=True
    elif kind=='unknown':snapshot['fields'].append({'key':None,'label':'Dato especial','kind':'input','required':True,'reference':'12345678'})
    elif kind=='long':snapshot['fields'][2]['max_length']=2
    else:snapshot['unsupported_required']=['Acepto las condiciones']
    assert app.state.billing.portal_preparation.fill_request(owner(app),ticket['id'],snapshot) is None
    assert 'fill' not in browser.calls


def test_replacement_keeps_manual_completion_and_removing_ticket_removes_all_linked_photos(billing_client):
    values,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    billing=app.state.billing;ticket=billing.store.list('ticket',owner(app))[0]
    source=ticket['document_id']
    replacement=billing.portal_preparation.replace_source(owner(app),ticket['id'],image('blue'))
    billing.complete_ticket_field(owner(app),ticket['id'],'store_number','00042',correction=True)
    assert billing.resolve_ticket(owner(app),ticket['id'])['store_number']=='00042'
    billing.delete_ticket(owner(app),ticket['id'])
    for identifier in (source,replacement['id']):
        with pytest.raises(AppError):billing.document(owner(app),identifier)
    assert len(billing.store.list('profile',owner(app)))==1


def test_same_purchase_in_different_photos_links_back_to_the_existing_ticket(billing_client):
    values,browser,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    billing=app.state.billing;first=billing.store.list('ticket',owner(app))[0]
    telegram.files['second']=image('blue');chat(document='second');chat(data=button(telegram,'an'))
    second=next(t for t in billing.store.list('ticket',owner(app)) if t['id']!=first['id'])
    record=billing.browser_review(owner(app),first['id'])
    review=billing.portal_permissions.request(owner(app),second['id'],route=True)
    billing.portal_permissions.decide(owner(app),review['id'],approve=True)
    assert client.portal.call(billing.portal_preparation.read_fields,owner(app),second['id'],record['snapshot'])
    assert billing.resolve_ticket(owner(app),second['id'])['id']==first['id']
    assert mail.sent==sat.calls==[]


def test_expired_browser_session_rebinds_fresh_controls_before_the_approved_fill(billing_client):
    values,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    approval=button(telegram,'xg')
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    record=app.state.billing.browser_review(owner(app),ticket['id'])
    previous=record['snapshot']['session_id']
    browser.manager.sessions[previous].created-=181
    chat(data=approval)
    assert 'Formulario llenado' in telegram.messages[-1][1] and browser.calls.count('fill')==1
    assert len(extractor.calls)==3
    assert app.state.billing.resolve_ticket(owner(app),ticket['id'])['portal_fill']['session_id']!=previous


def test_cached_data_can_be_filled_after_the_original_photo_expires(billing_client):
    values,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    with app.state.database.connection(True) as conn:
        conn.execute('UPDATE billing_records SET expires=? WHERE id=?',(time.time()-1,ticket['document_id']))
    chat(data='bn:'+ticket['id'])
    assert 'foto temporal venció' not in telegram.messages[-1][1] and len(extractor.calls)==3
    chat(data=button(telegram,'xg'))
    assert 'Formulario llenado' in telegram.messages[-1][1] and len(extractor.calls)==3


def test_explicit_email_without_a_portal_keeps_the_purchase_read_and_native_mail_review(billing_client):
    class EmailRouting(ScopedExtractor):
        async def extract_routing(self,mime,data):
            self.calls.append(('routing',mime))
            values={key:None for key in TicketExtraction.model_fields}
            for key in ('is_ticket','readable','merchant','billing_url','billing_email','billing_email_evidence','email_explicitly_for_billing'):
                values[key]=TICKET[key]
            return TicketExtraction.model_validate(values)
    extractor=EmailRouting();browser=RealBrowser()
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,browser=browser,extractor=extractor)
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    assert [item[0] for item in extractor.calls]==['csf','routing','fields']
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    assert ticket['folio']==TICKET['folio'] and ticket['total']==TICKET['total']
    assert any(data.startswith('vc:') for _,data in telegram.messages[-1][2])
    assert browser.calls==mail.sent==sat.calls==[]
