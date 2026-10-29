"""Regresiones de extremo a extremo para formularios y recuperación del ticket."""
import pytest

from backend.app.billing.models import TicketExtraction
from backend.app.billing.portal_preparation import portal_value
from backend.app.errors import AppError
from .billing_fakes import TICKET,image
from .fake_model import ScriptedModel,call
from .test_billing import billing_client
from .test_portal_preparation import RealBrowser,ScopedExtractor,prepared,ROOT,FORM
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile
from .test_telegram_workflow import choice


def test_an_old_upload_button_cannot_fill_using_data_from_the_previous_photo(billing_client):
    values,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='xu:'+ticket['id'])
    telegram.files['replacement']=image('blue');chat(document='replacement')
    chat(data=choice(telegram,'same'))
    buttons=telegram.messages[-1][2]
    assert any(data.startswith('xa:') for _,data in buttons)
    assert not any(data.startswith('xg:') for _,data in buttons)
    chat(data=button(telegram,'xa'))
    assert any(data.startswith('xg:') for _,data in telegram.messages[-1][2])
    assert len(extractor.calls)==4 and mail.sent==sat.calls==[]


def test_a_rejected_field_is_not_reported_as_an_unknown_fill(billing_client):
    values,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    async def rejected(*args):raise AppError(422,'browser_fill','La opción no está disponible.')
    browser.fill=rejected
    chat(data=button(telegram,'xg'))
    review=app.state.billing.store.list('portal_fill_review',owner(app))[0]
    assert review['_state']=='rejected'
    assert 'No se confirmó el llenado' not in telegram.messages[-1][1]
    assert mail.sent==sat.calls==[]


def test_confirming_a_lost_fill_response_updates_the_ticket_as_well_as_the_review(billing_client):
    values,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    original=browser.fill
    async def uncertain(*args):
        await original(*args)
        raise AppError(504,'browser_timeout','Respuesta perdida.')
    browser.fill=uncertain
    chat(data=button(telegram,'xg'));chat(data=button(telegram,'xs'))
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    assert ticket['portal_fill']['state']=='complete' and ticket['portal_fill']['network_frozen']
    assert browser.calls.count('fill')==1 and mail.sent==sat.calls==[]


@pytest.mark.parametrize('expired',['review','browser'])
def test_expired_fill_keeps_an_owned_recoverable_status_instead_of_a_missing_record(billing_client,expired):
    values,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'xg'))
    status=button(telegram,'xs');review_id=status.split(':')[1]
    if expired=='review':
        with app.state.database.connection(True) as conn:
            conn.execute('UPDATE billing_records SET expires=0 WHERE id=?',(review_id,))
    else:
        async def gone(*args):raise AppError(404,'browser_session','Sesión vencida.')
        browser.filled=gone
    chat(data=status)
    assert 'venció' in telegram.messages[-1][1] and 'no encontrado' not in telegram.messages[-1][1]
    assert app.state.billing.store.list('ticket',owner(app))[0]['portal_fill']['state']=='expired'
    before=list(browser.calls);chat(data=status,user=456)
    assert browser.calls==before and 'no encontrado' in telegram.messages[-1][1]


@pytest.mark.parametrize('label,expected',[
    ('Fecha dd-mm-aaaa (y/o referencia)','24-10-2026'),
    ('Purchase date mm/dd/yyyy','10/24/2026'),
    ('Fecha yyyy.mm.dd','2026.10.24'),
])
def test_date_uses_the_explicit_format_of_the_actual_control(label,expected):
    assert portal_value({'key':'purchase_date','label':label},'2026-10-24')==expected


def alternative_flow(factory,*,scope='portal-fields-v2',known_id=None,include_account=True,primary_label='Web ID',ambiguous=False,linked=False):
    html='''<form><label>RFC<input required></label><label>Web ID<input required></label></form>
        <button type="button" aria-expanded="false" onclick="document.getElementById('other').hidden=false;this.setAttribute('aria-expanded','true')">Si no cuenta con Web ID ingrese los datos del ticket</button>
        <section id="other" hidden><form><label>Folio<input required></label><label>Cuenta<input required></label>
        <label>Fecha dd/mm/aaaa<input required></label><button>Registrar</button></form></section>'''
    if not include_account:html=html.replace('<label>Cuenta<input required></label>','')
    html=html.replace('Web ID',primary_label)
    if ambiguous:
        html+='<button type="button" aria-expanded="false" onclick="document.getElementById(\'other\').hidden=false;this.setAttribute(\'aria-expanded\',\'true\')">Sin '+primary_label+', capturar de otra forma</button>'
    class AlternativeExtractor(ScopedExtractor):
        async def extract_fields(self,mime,data,fields):
            self.calls.append(('fields',[{'key':f['key'],'label':f['label']} for f in fields]))
            return TicketExtraction(**(TICKET|{'merchant':'Pizza Alfa','account_number':'AB-004','web_id':known_id}))
    browser=RealBrowser()
    if linked:
        target='https://another-provider.example/receipt'
        browser.pages.pages[FORM]='<form><label>RFC<input required></label><label>Web ID<input required></label></form><a href="'+target+'">No tengo Web ID, ingresar los datos del ticket</a>'
        browser.pages.pages[target]='<form><label>Folio<input required></label><label>Cuenta<input required></label><label>Fecha dd/mm/aaaa<input required></label></form>'
    else:browser.pages.pages[FORM]=html
    extractor=AlternativeExtractor();model=ScriptedModel([[call('portal_browser_observe')]])
    values=create(factory,model,browser=browser,extractor=extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('red');chat(document='receipt')
    document=app.state.billing.document(owner(app),app.state.billing.store.list('document',owner(app))[0]['id'])
    app.state.billing.store.update('document',owner(app),document['id'],document|{'analysis_scope_offered':scope})
    chat(data=button(telegram,'an'));chat(data=button(telegram,'wa'))
    return values,browser,model


def test_missing_web_id_uses_the_observed_alternative_then_reads_its_fields(billing_client):
    values,browser,model=alternative_flow(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    targets=[{f['key'] for f in fields} for kind,fields in extractor.calls if kind=='fields']
    assert targets==[{'web_id'},{'folio','account_number','purchase_date'}]
    assert 'AB-004' in telegram.messages[-1][1] and 'xg:' in str(telegram.messages[-1][2])
    assert 'Web ID: Pendiente' not in telegram.messages[-1][1]
    before=list(browser.pages.calls);chat(data=button(telegram,'xg'))
    assert browser.pages.calls==before and browser.calls.count('fill')==1
    assert len(model.inputs)==1 and mail.sent==sat.calls==[]


def test_an_old_two_read_consent_needs_a_button_before_reading_new_alternative_fields(billing_client):
    values,browser,model=alternative_flow(billing_client,scope='portal-fields-v1')
    client,app,extractor,mail,sat,telegram,chat=values
    assert len([c for c in extractor.calls if c[0]=='fields'])==1
    assert 'xa:' in str(telegram.messages[-1][2]) and 'xg:' not in str(telegram.messages[-1][2])
    chat(data=button(telegram,'xa'))
    assert len([c for c in extractor.calls if c[0]=='fields'])==2
    assert 'xg:' in str(telegram.messages[-1][2]) and len(model.inputs)==1


def test_an_alternative_reuses_known_receipt_data_instead_of_another_reading(billing_client):
    values,browser,model=alternative_flow(billing_client,include_account=False)
    client,app,extractor,mail,sat,telegram,chat=values
    assert len([c for c in extractor.calls if c[0]=='fields'])==1
    assert 'xg:' in str(telegram.messages[-1][2]) and 'Web ID: Pendiente' not in telegram.messages[-1][1]
    assert browser.calls.count('act')==2


def test_a_readable_web_id_keeps_the_original_method_and_does_not_open_an_alternative(billing_client):
    values,browser,model=alternative_flow(billing_client,known_id='ID-00042')
    client,app,extractor,mail,sat,telegram,chat=values
    assert 'ID-00042' in telegram.messages[-1][1] and browser.calls.count('act')==1
    assert len([c for c in extractor.calls if c[0]=='fields'])==1
    assert 'xg:' in str(telegram.messages[-1][2])


def test_identical_photo_reupload_preserves_the_alternative_and_does_not_charge_again(billing_client):
    values,browser,model=alternative_flow(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    count=len(extractor.calls)
    chat(data='xu:'+ticket['id']);chat(document='receipt')
    chat(data=choice(telegram,'same'))
    assert len(extractor.calls)==count and 'xg:' in str(telegram.messages[-1][2])
    assert 'Web ID: Pendiente' not in telegram.messages[-1][1]


def test_human_data_entered_during_navigation_is_not_overwritten_by_an_old_ticket_copy(billing_client):
    def edit_while_opening(app,ticket):
        billing=app.state.billing;original=billing.browser.act
        async def opening(*args):
            current=billing.resolve_ticket(owner(app),ticket['id'])
            current['store_number']='00042';current['fields_completed_by_user']=['store_number']
            billing.store.update('ticket',owner(app),current['id'],current)
            return await original(*args)
        billing.browser.act=opening
    values,browser,model=prepared(billing_client,before_portal=edit_while_opening)
    client,app,extractor,mail,sat,telegram,chat=values
    assert app.state.billing.store.list('ticket',owner(app))[0]['store_number']=='00042'
    assert '00042' in telegram.messages[-1][1]


def test_a_navigation_failure_also_preserves_a_concurrent_human_correction(billing_client):
    def edit_before_failure(app,ticket):
        billing=app.state.billing
        async def failed(*args):
            current=billing.resolve_ticket(owner(app),ticket['id'])
            current['store_number']='00042';current['fields_completed_by_user']=['store_number']
            billing.store.update('ticket',owner(app),current['id'],current)
            raise AppError(502,'browser_navigation','Fallo de navegación simulado.')
        billing.browser.act=failed
    values,browser,model=prepared(billing_client,before_portal=edit_before_failure)
    client,app,extractor,mail,sat,telegram,chat=values
    assert app.state.billing.store.list('ticket',owner(app))[0]['store_number']=='00042'
    assert mail.sent==sat.calls==[]


def test_another_identifier_uses_its_own_observed_alternative_not_a_web_id_rule(billing_client):
    values,browser,model=alternative_flow(billing_client,primary_label='Referencia')
    client,app,extractor,mail,sat,telegram,chat=values
    targets=[{f['key'] for f in fields} for kind,fields in extractor.calls if kind=='fields']
    assert targets==[{'reference_number'},{'folio','account_number','purchase_date'}]
    assert 'xg:' in str(telegram.messages[-1][2]) and len(model.inputs)==1


def test_an_alternative_can_be_an_observed_link_to_another_provider(billing_client):
    values,browser,model=alternative_flow(billing_client,linked=True)
    client,app,extractor,mail,sat,telegram,chat=values
    assert browser.pages.calls[-1][0]=='https://another-provider.example/receipt'
    assert 'AB-004' in telegram.messages[-1][1] and 'xg:' in str(telegram.messages[-1][2])
    assert len(model.inputs)==1 and mail.sent==sat.calls==[]


def test_ambiguous_public_alternatives_require_selection_then_use_only_the_selected_fields(billing_client):
    values,browser,model=alternative_flow(billing_client,ambiguous=True)
    client,app,extractor,mail,sat,telegram,chat=values
    assert browser.calls.count('act')==1 and len(extractor.calls)==3
    choices=[data for _,data in telegram.messages[-1][2] if data.startswith('ba:')]
    assert len(choices)==2
    chat(data=choices[0])
    assert browser.calls.count('act')==2 and 'xg:' in str(telegram.messages[-1][2])
    assert 'Web ID: Pendiente' not in telegram.messages[-1][1]
    assert len(model.inputs)==1


@pytest.mark.parametrize('label,key,date_label,expected',[
    ('Cuenta','account_number','Fecha dd/mm/aaaa','24/10/2026'),
    ('Referencia','reference_number','Purchase date mm-dd-yyyy','10-24-2026'),
    ('Código de facturación','billing_code','Fecha yyyy.mm.dd','2026.10.24'),
])
def test_distinct_portals_extract_the_requested_identifier_and_fill_their_own_date_format(billing_client,label,key,date_label,expected):
    class Receipt(ScopedExtractor):
        async def extract_fields(self,mime,data,fields):
            self.calls.append(('fields',[{'key':f['key'],'label':f['label']} for f in fields]))
            return TicketExtraction(**(TICKET|{'merchant':'Pizza Alfa','account_number':'ACCOUNT-02',
                'reference_number':'REF-03','billing_code':'CODE-04'}))
    browser=RealBrowser();browser.pages.pages[FORM]='<form><label>RFC<input required></label><label>'+label+'<input required></label><label>'+date_label+'<input required></label></form>'
    model=ScriptedModel([[call('portal_browser_observe')]])
    values=create(billing_client,model,browser=browser,extractor=Receipt())
    client,app,extractor,mail,sat,telegram,chat=values
    confirm_profile(telegram,chat);telegram.files['receipt']=image('red')
    chat(document='receipt');chat(data=button(telegram,'an'));chat(data=button(telegram,'wa'))
    assert {f['key'] for f in extractor.calls[-1][1]}=={key,'purchase_date'}
    before=list(browser.pages.calls);chat(data=button(telegram,'xg'))
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    session=browser.manager.sessions[ticket['portal_fill']['session_id']]
    actual=client.portal.call(session.page.locator('input').evaluate_all,'nodes=>nodes.map(e=>e.value)')
    assert actual[1:]==[{'account_number':'ACCOUNT-02','reference_number':'REF-03','billing_code':'CODE-04'}[key],expected]
    assert browser.pages.calls==before and len(extractor.calls)==3 and len(model.inputs)==1
    assert mail.sent==sat.calls==[]
