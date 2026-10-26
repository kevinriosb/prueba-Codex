import base64
import io
import json

import httpx2
import pytest
import zxingcpp
from openai import AuthenticationError, RateLimitError
from PIL import Image

from backend.app.billing.extraction import DocumentExtractor, EXTRACTION_VERSION
from backend.app.billing.models import TicketExtraction
from backend.app.billing.receipt import portal_url
from backend.app.errors import AppError
from .billing_fakes import FISCAL, TICKET, image
from .fake_model import ScriptedModel, answer
from .test_billing import billing_client, profile, ticket
from .test_telegram_agent import button, create, owner
from .test_telegram_documents import confirm_profile


def qr_image(url):
    qr=zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode,url,width=400,height=400)
    view=memoryview(qr)
    picture=Image.frombytes('L',(view.shape[1],view.shape[0]),view.tobytes())
    encoded=io.BytesIO()
    picture.save(encoded,format='PNG')
    return encoded.getvalue()


@pytest.mark.parametrize('url,expected',[
    ('facturas.comercio.example/sucursal','https://facturas.comercio.example/sucursal'),
    ('https://facturas.example.com/sucursal?folio=ABC','https://facturas.example.com/sucursal?folio=ABC'),
    ('http://facturas.example.com','http://facturas.example.com'),
    ('javascript:alert(1)',None),('https://127.0.0.1/admin',None),('https://169.254.169.254/',None),
    ('https://user:password@example.com',None),('https://localhost/admin',None),
    ('https://0177.0.0.1/',None),('https://example.com/ bad',None)])
def test_visible_portal_url_is_normalized_without_network(url,expected):
    assert portal_url(url)==expected


def test_one_invalid_optional_field_does_not_discard_receipt_or_portal():
    parsed=TicketExtraction(**(TICKET|{'billing_url':'facturas.example.com/local',
        'purchase_date':'no legible','total':'$1,235.00 MXN'}))
    assert parsed.billing_url=='https://facturas.example.com/local'
    assert parsed.total=='1235.00' and parsed.purchase_date is None
    assert parsed.merchant==TICKET['merchant'] and parsed.folio==TICKET['folio']
    assert TicketExtraction(**(TICKET|{'billing_url':'javascript:alert(1)'})).billing_url is None


def test_sdk_accepts_receipt_with_printed_url_missing_scheme(billing_client):
    model=ScriptedModel([[answer(json.dumps(TICKET|{'billing_url':'facturas.example.com/local','total':'$635.00'}))]])
    client,app,*_=billing_client(model=model)
    extractor=DocumentExtractor(app.state.service,app.state.settings)
    parsed=client.portal.call(extractor.extract,'ticket','image/jpeg',image())
    assert parsed.billing_url=='https://facturas.example.com/local' and parsed.total=='635.00'
    assert len(model.inputs)==1


def test_portal_ticket_shows_url_and_owned_fiscal_data_without_claiming_invoice(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    confirm_profile(telegram,chat)
    extractor.ticket.update(billing_url='facturas.example.com/local',billing_email=None,email_explicitly_for_billing=False)
    telegram.files['receipt']=image('green')
    chat(document='receipt')
    chat(data=button(telegram,'an'))
    assert 'https://facturas.example.com/local' in telegram.messages[-1][1]
    assert 'pendiente de automatizar' in telegram.messages[-1][1]
    action=button(telegram,'bp')
    chat(data=action,user=456)
    assert FISCAL['rfc'] not in telegram.messages[-1][1]
    chat(data=action)
    assert all(value in telegram.messages[-1][1] for value in (FISCAL['rfc'],FISCAL['name'],'G03',TICKET['folio']))
    assert 'No se ha solicitado la factura' in telegram.messages[-1][1]
    assert mail.sent==sat.calls==[]


def test_partial_receipt_is_kept_when_portal_is_visible(billing_client):
    client,app,extractor,*_=billing_client()
    extractor.ticket.update(readable=False,folio=None,billing_url='facturas.example.com',billing_email=None)
    item=ticket(client)
    assert item['status']=='REQUIERE_INTERVENCION' and item['merchant']==TICKET['merchant']
    assert item['channels']['method']=='portal_manual' and item['billing_url']=='https://facturas.example.com'


def test_qr_is_decoded_locally_and_used_only_with_billing_evidence(billing_client):
    client,app,extractor,*_=billing_client()
    selected=profile(client)['id']
    extractor.ticket.update(billing_url=None,billing_email=None,email_explicitly_for_billing=False,
        billing_url_evidence='Escanea para facturar')
    response=client.post('/api/billing/documents',params={'purpose':'ticket','profile_id':selected},
        content=qr_image('https://facturas.example.com/qr?folio=DEMO'))
    result=client.post('/api/billing/documents/'+response.json()['id']+'/analyze',json={'consent_to_ai':True})
    assert result.status_code==200,result.text
    item=result.json()['ticket']
    assert item['billing_url']=='https://facturas.example.com/qr?folio=DEMO'
    assert item['channels']['method']=='portal_manual'
    assert len(extractor.calls)==2


def test_survey_qr_is_not_automatically_used_as_billing_portal(billing_client):
    client,app,extractor,*_=billing_client()
    selected=profile(client)['id']
    extractor.ticket.update(billing_url=None,billing_email=None,email_explicitly_for_billing=False,
        billing_url_evidence='Califica nuestro servicio')
    response=client.post('/api/billing/documents',params={'purpose':'ticket','profile_id':selected},
        content=qr_image('https://encuesta.example.com/'))
    result=client.post('/api/billing/documents/'+response.json()['id']+'/analyze',json={'consent_to_ai':True})
    item=result.json()['ticket']
    assert item['billing_url'] is None and item['channels']['method']=='manual'
    assert item['qr_urls']==['https://encuesta.example.com/']


def test_email_route_prepares_csf_and_ticket_then_sends_only_after_owner_confirms(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,billing_live_email_enabled=True)
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('yellow')
    chat(document='receipt')
    chat(data=button(telegram,'an'))
    assert TICKET['billing_email'] in telegram.messages[-1][1]
    action=button(telegram,'vc')
    chat(data=action,user=456)
    assert mail.sent==[]
    chat(data=action)
    review=telegram.messages[-1][1]
    assert 'Constancia de Situación Fiscal' in review and 'Ticket de compra' in review
    assert FISCAL['rfc'] in review and TICKET['billing_email'] in review
    assert 'por tu elección' in review and mail.sent==[]
    confirm=button(telegram,'se')
    chat(data=confirm,user=456)
    assert mail.sent==[]
    chat(data=confirm)
    assert len(mail.sent)==1 and len(list(mail.sent[0][1].iter_attachments()))==2
    chat(data=confirm)
    assert len(mail.sent)==1


def test_generic_support_address_does_not_get_email_send_buttons(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    confirm_profile(telegram,chat)
    extractor.ticket.update(billing_email='soporte@example.com',email_explicitly_for_billing=False,
        billing_email_evidence='Escríbenos para sugerencias: soporte@example.com')
    telegram.files['receipt']=image('yellow')
    chat(document='receipt')
    chat(data=button(telegram,'an'))
    assert not any(data.startswith(('vc:','ec:','pr:')) for _,data in telegram.messages[-1][2])
    assert mail.sent==[]


@pytest.mark.parametrize('error,code',[
    (AuthenticationError('SENSITIVE-PROVIDER-BODY',response=httpx2.Response(401,request=httpx2.Request('POST','https://api.openai.com/v1/responses')),body={}), 'extraction_auth'),
    (RateLimitError('SENSITIVE-PROVIDER-BODY',response=httpx2.Response(429,request=httpx2.Request('POST','https://api.openai.com/v1/responses')),body={}), 'extraction_quota'),
    (TimeoutError('SENSITIVE-PROVIDER-BODY'),'extraction_timeout')])
def test_extraction_error_distinguishes_provider_from_legibility_without_leaking_body(billing_client,error,code):
    model=ScriptedModel(failure=error)
    client,app,*_=billing_client(model=model)
    extractor=DocumentExtractor(app.state.service,app.state.settings)
    with pytest.raises(AppError) as caught:
        client.portal.call(extractor.extract,'ticket','image/jpeg',image())
    assert caught.value.code==code and 'SENSITIVE' not in caught.value.message


def test_legacy_failed_receipt_can_be_reanalyzed_once_with_same_bytes_and_expiry(billing_client):
    model=ScriptedModel()
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model)
    confirm_profile(telegram,chat)
    selected=app.state.telegram._state(owner(app))['active_profile']
    source=app.state.billing.receive_document(owner(app),'ticket',image('red'),profile_id=selected)
    old=app.state.billing.document(owner(app),source['id'])
    ticket_id=old['ticket_id']
    record=app.state.billing.store.get('ticket',owner(app),ticket_id)
    record.update(status='ERROR',note='Falló el análisis anterior.')
    app.state.billing.store.update('ticket',owner(app),ticket_id,record)
    state=app.state.telegram._state(owner(app))
    state.update(last_document_id=old['id'],last_ticket_id=ticket_id)
    app.state.telegram._save_state(owner(app),state)
    chat('Por qué no es fiable?')
    assert 'Falló el análisis anterior' in telegram.messages[-1][1] and model.inputs==[]
    action=button(telegram,'ra')
    chat(data=action,user=456)
    assert len(extractor.calls)==1
    chat(data=action)
    current=app.state.billing.document(owner(app),app.state.telegram._state(owner(app))['last_document_id'])
    assert current['id']!=old['id'] and current['data']==old['data'] and current['_expires']<=old['_expires']
    assert current['extraction_version']==EXTRACTION_VERSION and len(extractor.calls)==2
    chat(data=action)
    assert len(extractor.calls)==2 and mail.sent==[]


def test_disabled_gmail_shows_preview_without_send_button(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('yellow')
    chat(document='receipt')
    chat(data=button(telegram,'an'))
    chat(data=button(telegram,'vc'))
    assert 'Vista previa' in telegram.messages[-1][1]
    assert not any(value.startswith('se:') for _,value in telegram.messages[-1][2])
    assert mail.sent==[]


def test_missing_purchase_field_can_be_completed_then_email_prepared_without_new_ai(billing_client):
    model=ScriptedModel()
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,billing_live_email_enabled=True)
    confirm_profile(telegram,chat)
    extractor.ticket['purchase_date']=None
    telegram.files['receipt']=image('red')
    chat(document='receipt')
    chat(data=button(telegram,'an'))
    action=next(value for _,value in telegram.messages[-1][2] if value.endswith(':purchase_date'))
    chat(data=action,user=456)
    chat(data=action)
    chat('/dato_ticket 2026-10-24')
    identifier=app.state.telegram._state(owner(app))['last_ticket_id']
    record=app.state.billing.store.get('ticket',owner(app),identifier)
    assert record['purchase_date']=='2026-10-24' and record['status']=='DATOS_EXTRAIDOS'
    chat(data=button(telegram,'vc'))
    assert button(telegram,'se') and mail.sent==[]
    assert len(extractor.calls)==2 and model.inputs==[]


def test_new_csf_diagnostic_does_not_reuse_previous_ticket_error(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('red')
    chat(document='receipt')
    chat(data=button(telegram,'an'))
    extractor.fiscal['postal_code']=None
    telegram.files['new-csf']=image('yellow')
    chat(document='new-csf',caption='/csf')
    chat(data=button(telegram,'an'))
    chat('Por qué ilegible?')
    assert 'CP fiscal' in telegram.messages[-1][1]
    assert 'Folio:' not in telegram.messages[-1][1]


def test_ticket_metadata_remains_available_after_original_document_expires(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('red')
    chat(document='receipt')
    chat(data=button(telegram,'an'))
    state=app.state.telegram._state(owner(app))
    with app.state.database.connection() as connection:
        connection.execute('UPDATE billing_records SET expires=0 WHERE id=?',(state['last_document_id'],))
    chat('/tickets')
    assert 'Folio: '+TICKET['folio'] in telegram.messages[-1][1]
    assert 'temporal venció' in telegram.messages[-1][1]
