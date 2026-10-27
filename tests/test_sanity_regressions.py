"""Casos de recuperación y consistencia descubiertos en la revisión general."""
import pytest

from backend.app.billing.models import Status
from backend.app.billing.providers import Attachment
from .billing_fakes import pdf,xml
from .test_billing import billing_client,response_for,sent


def test_conflicting_xmls_in_one_reply_are_rejected_before_storing_any_attachment(billing_client):
    client,app,_,mail,sat,_=billing_client(billing_live_email_enabled=True)
    item=sent(client)
    conflicting=xml().replace(b'11111111-1111-4111-8111-111111111111',b'22222222-2222-4222-8222-222222222222')
    mail.replies=[response_for(item,[Attachment('one.xml','application/xml',xml()),
        Attachment('two.xml','application/xml',conflicting)])]
    before=app.state.billing.store.count('document','owner')
    response=client.post('/api/billing/tickets/'+item['id']+'/track',json={})
    assert response.status_code==409
    assert response.json()['error']['code']=='conflicting_invoice'
    assert app.state.billing.store.count('document','owner')==before
    assert app.state.billing.store.list('invoice_uuid','owner')==[]
    assert app.state.billing.store.list('mail_reply','owner')==[]
    assert sat.calls==[]


def test_bad_attachment_does_not_leave_partial_files_or_mark_reply_processed(billing_client):
    client,app,_,mail,sat,_=billing_client(billing_live_email_enabled=True)
    item=sent(client)
    mail.replies=[response_for(item,[Attachment('invoice.pdf','application/pdf',pdf()),
        Attachment('invalid.xml','application/xml',b'<invalid/>')])]
    before=app.state.billing.store.count('document','owner')
    path='/api/billing/tickets/'+item['id']+'/track'
    assert client.post(path,json={}).status_code==422
    assert app.state.billing.store.count('document','owner')==before
    assert app.state.billing.store.list('mail_reply','owner')==[]
    # Una revisión posterior no debe saltarse silenciosamente el correo fallido.
    assert client.post(path,json={}).status_code==422
    assert app.state.billing.store.count('document','owner')==before
    assert sat.calls==[]


@pytest.mark.parametrize('query_success',[True,False])
def test_negative_sat_recheck_invalidates_previous_verdict_before_later_pdf(billing_client,query_success):
    client,app,_,mail,sat,_=billing_client(billing_live_email_enabled=True)
    item=sent(client)
    mail.replies=[response_for(item,[Attachment('invoice.xml','application/xml',xml())])]
    path='/api/billing/tickets/'+item['id']
    first=client.post(path+'/track',json={'authorize_sat':True})
    assert first.status_code==200 and first.json()['validation']['sat_verified'] is True
    async def not_current(document):
        return {'query_success':query_success,'vigente':False,'status':'Cancelado' if query_success else 'Desconocido'}
    sat.verify=not_current
    second=client.post(path+'/validate',json={'authorize_sat':True})
    assert second.status_code==200 and second.json()['validation']['sat_verified'] is False
    later=response_for(item,[Attachment('invoice.pdf','application/pdf',pdf())])
    later.identifier='later-pdf-after-sat-change'
    mail.replies=[later]
    result=client.post(path+'/track',json={})
    assert result.status_code==200
    assert result.json()['status']!=Status.FACTURACION_COMPLETADA
    assert result.json()['validation']['sat_verified'] is False
