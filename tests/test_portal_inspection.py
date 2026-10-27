import json

import httpx2
import pytest

from backend.app.billing.models import TicketExtraction
from backend.app.billing.portal_inspection import FormFields, PortalInspector, portal_plan
from backend.app.billing.receipt import missing_receipt_fields, pending_receipt_fields
from .billing_fakes import FISCAL, TICKET, image
from .fake_model import ScriptedModel, answer
from .test_billing import billing_client, prepare, profile, ticket
from .test_telegram_agent import button, create, owner
from .test_telegram_documents import confirm_profile
from .test_adaptive_browser import grant


PAGE='https://f.zetus.mx/albat'
FORM='<form method="post"><label for="key">Cuenta:</label><input id="key" name="account"><input type="hidden" name="token" value="MERCHANT-PRIVATE"><button>Siguiente</button></form>'


async def public_dns(_host):
    return ['8.8.8.8']


def install(app, handler, resolver=public_dns):
    app.state.billing.portal_inspector=PortalInspector(app.state.settings,
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),resolver=resolver)
    return app.state.billing.portal_inspector


@pytest.mark.parametrize('markup,expected',[
    (FORM,['account_number']),
    ('<label>Cuenta<input name="entry"></label><label>Folio<input name="folio"></label>', ['account_number','folio']),
    ('<input aria-label="Número de referencia"><input placeholder="Código de facturación">',['reference_number','billing_code']),
    ('<input type="password" name="account"><script>"<input name=cuenta>"</script>',[])])
def test_form_parser_distinguishes_labels_and_ignores_sensitive_or_script_controls(markup,expected):
    parser=FormFields()
    parser.feed(markup)
    assert [item['key'] for item in parser.fields()]==expected


def test_account_is_extracted_as_its_own_value_with_real_sdk(billing_client):
    raw=TICKET|{'account_number':'9DEMO42AZC','folio':'20379','reference_number':'REF-8'}
    model=ScriptedModel([[answer(json.dumps(raw))]])
    client,app,*_=billing_client(model=model)
    from backend.app.billing.extraction import DocumentExtractor
    parsed=client.portal.call(DocumentExtractor(app.state.service,app.state.settings).extract,'ticket','image/jpeg',image())
    assert parsed.account_number=='9DEMO42AZC' and parsed.folio=='20379' and parsed.reference_number=='REF-8'
    assert len(model.inputs)==1


def test_public_form_is_read_before_plan_and_never_receives_purchase_or_fiscal_data(billing_client):
    requests=[]
    client,app,extractor,mail,*_=billing_client()
    def handler(request):
        requests.append(request)
        assert request.method=='GET' and str(request.url)=='https://8.8.8.8/albat'
        assert request.headers['host']=='f.zetus.mx' and request.extensions['sni_hostname']=='f.zetus.mx'
        assert not request.content and not request.url.query
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        return httpx2.Response(200,text=FORM,headers={'content-type':'text/html'})
    inspector=install(app,handler)
    extractor.ticket.update(billing_url=PAGE,account_number='9DEMO42AZC',folio='20379')
    item=ticket(client)
    assert 'portal_inspection' not in item and requests==[]
    grant(client,item['id'],intent='details')
    result=client.post('/api/billing/tickets/'+item['id']+'/inspect-portal')
    assert result.status_code==200
    plan=result.json()['portal_inspection']
    assert plan['status']=='verified' and plan['source']=='public_html'
    assert plan['fields']==[{'key':'account_number','label':'Cuenta','value':'9DEMO42AZC','matched':True}]
    assert plan['alternatives'][0]['label']=='Folio'
    assert plan['purchase_validated'] is False and plan['submitted'] is False
    assert plan['lookup_performed'] is False and mail.sent==[]
    assert len(requests)==1 and 'MERCHANT-PRIVATE' not in json.dumps(plan)
    assert FISCAL['rfc'] not in json.dumps(inspector.cache)


@pytest.mark.parametrize('addresses',[
    ['127.0.0.1'],['169.254.169.254'],['::1'],['8.8.8.8','10.0.0.1']])
def test_private_or_mixed_dns_never_makes_an_http_request(billing_client,addresses):
    client,app,*_=billing_client()
    calls=[]
    async def resolver(_host):
        return addresses
    def handler(request):
        calls.append(request)
        pytest.fail('No debe consultar una IP interna')
    inspector=install(app,handler,resolver)
    result=client.portal.call(inspector.inspect,PAGE)
    assert calls==[] and result['status']=='template'
    assert result['source']=='user_screenshot_template' and result['lookup_performed'] is False


@pytest.mark.parametrize('url',[
    'https://f.zetus.mx.evil.example/albat','https://f.zetus.mx/private',
    'http://f.zetus.mx/albat','https://127.0.0.1/albat','https://user:secret@f.zetus.mx/albat'])
def test_only_approved_public_pages_can_be_inspected(billing_client,url):
    client,app,*_=billing_client()
    async def resolver(_host):
        pytest.fail('No debe resolver un destino no aprobado')
    inspector=install(app,lambda _:pytest.fail('No debe llamar a HTTP'),resolver)
    result=client.portal.call(inspector.inspect,url)
    assert result['status']=='unsupported' and result['fields']==[]


def test_redirect_cannot_leave_approved_page_or_forward_query_data(billing_client):
    client,app,*_=billing_client()
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx2.Response(302,headers={'location':'https://127.0.0.1/private'})
    inspector=install(app,handler)
    result=client.portal.call(inspector.inspect,PAGE+'?rfc=PRIVATE&account=SECRET')
    assert len(calls)==1 and not calls[0].url.query
    assert result['status']=='template' and 'PRIVATE' not in json.dumps(result)


def test_safe_trailing_slash_redirect_can_be_followed_once(billing_client):
    client,app,*_=billing_client()
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        if request.url.path=='/albat':
            return httpx2.Response(301,headers={'location':'/albat/'})
        return httpx2.Response(200,text=FORM,headers={'content-type':'text/html'})
    inspector=install(app,handler)
    result=client.portal.call(inspector.inspect,PAGE)
    assert calls==['/albat','/albat/'] and result['status']=='verified'


def test_missing_account_never_silently_uses_folio_as_account():
    result=portal_plan({'folio':'20379','reference_number':'REF-8'},
        {'status':'verified','fields':[{'key':'account_number','label':'Cuenta'}]})
    assert result['fields'][0]['value'] is None and result['fields'][0]['matched'] is False
    assert result['proposed_reference']['value']=='20379' and result['proposed_reference']['label']=='Folio'
    assert result['purchase_validated'] is False


def test_unavailable_form_template_is_marked_as_unverified_not_live_validation(billing_client):
    client,app,*_=billing_client()
    def handler(_request):
        raise httpx2.ConnectError('PRIVATE-NETWORK-DETAILS')
    inspector=install(app,handler)
    result=client.portal.call(inspector.inspect,PAGE)
    assert result['status']=='template' and result['fields'][0]['key']=='account_number'
    assert 'PRIVATE' not in json.dumps(result)


def test_account_only_receipt_can_prepare_email_with_correct_label(billing_client):
    client,app,extractor,*_=billing_client()
    extractor.ticket.update(account_number='9DEMO42AZC',folio=None)
    item=ticket(client)
    assert missing_receipt_fields(item)==[] and item['status']=='DATOS_EXTRAIDOS'
    approval=prepare(client,item)
    assert 'Cuenta: 9DEMO42AZC' in approval['body'] and 'Folio: None' not in approval['body']
    assert 'Cuenta 9DEMO42AZC' in approval['subject']


def test_portal_that_requires_account_does_not_ask_for_an_unneeded_folio():
    data=TICKET|{'folio':None,'account_number':None,
        'portal_inspection':{'fields':[{'key':'account_number','label':'Cuenta'}]}}
    assert pending_receipt_fields(data)==['account_number']


def test_case_sensitive_accounts_remain_different_purchase_references(billing_client):
    client,app,extractor,*_=billing_client()
    selected=profile(client)['id']
    extractor.ticket.update(folio=None,account_number='case-A')
    first=ticket(client,selected,color='red')
    extractor.ticket['account_number']='case-a'
    second=ticket(client,selected,color='blue')
    assert first['id']!=second['id']


def test_telegram_shows_account_and_can_complete_it_without_asking_for_folio(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    install(app,lambda _:httpx2.Response(200,text=FORM,headers={'content-type':'text/html'}))
    confirm_profile(telegram,chat)
    extractor.ticket.update(billing_url=PAGE,account_number=None,folio='20379')
    telegram.files['receipt']=image('red')
    chat(document='receipt')
    chat(data=button(telegram,'an'))
    action=button(telegram,'bp')
    chat(data=action)
    assert '¿Permites revisar este portal?' in telegram.messages[-1][1]
    chat(data=button(telegram,'wa'))
    chat('/estado')
    assert 'Portal pide: Cuenta' in telegram.messages[-1][1]
    assert 'Folio: 20379' in telegram.messages[-1][1]
    fill=next(value for _,value in telegram.messages[-1][2] if value.endswith(':account_number'))
    chat(data=fill,user=456)
    assert mail.sent==[]
    chat(data=fill)
    chat('/dato_ticket 9DEMO42AZC')
    assert 'Cuenta: 9DEMO42AZC' in telegram.messages[-1][1] and 'Falta: Cuenta' not in telegram.messages[-1][1]
    action=button(telegram,'bp')
    chat(data=action)
    assert 'Cuenta: 9DEMO42AZC' in telegram.messages[-1][1]
    assert 'No se ha solicitado la factura' in telegram.messages[-1][1] and len(extractor.calls)==2


def test_legacy_successful_receipt_can_refresh_missing_account_once_without_duplicate_request(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    install(app,lambda _:httpx2.Response(200,text=FORM,headers={'content-type':'text/html'}))
    confirm_profile(telegram,chat)
    extractor.ticket.update(billing_url=PAGE,account_number=None,folio='20379')
    telegram.files['receipt']=image('red')
    chat(document='receipt')
    chat(data=button(telegram,'an'))
    state=app.state.telegram._state(owner(app))
    previous=app.state.billing.document(owner(app),state['last_document_id'])
    previous['extraction_version']='document-previous'
    app.state.billing.store.update('document',owner(app),previous['id'],previous)
    chat(data=button(telegram,'bp'))
    chat(data=button(telegram,'wa'))
    chat('/tickets')
    retry=button(telegram,'ra')
    extractor.ticket['account_number']='9DEMO42AZC'
    chat(data=retry)
    current=app.state.telegram._state(owner(app))
    assert current['last_ticket_id']!=previous['ticket_id']
    assert 'Cuenta: 9DEMO42AZC' in telegram.messages[-1][1]
    assert app.state.billing.document(owner(app),current['last_document_id'])['data']==previous['data']
    assert len(extractor.calls)==3 and mail.sent==[]
    chat(data=retry)
    assert len(extractor.calls)==3


def test_portal_inspection_endpoint_is_owned_and_purchase_edit_invalidates_email(billing_client):
    client,app,extractor,mail,*_=billing_client(billing_live_email_enabled=True)
    selected=profile(client)['id']
    item=ticket(client,selected)
    approval=prepare(client,item)
    changed=app.state.billing.store.get('ticket','owner',item['id'])
    changed['account_number']='NEW-ACCOUNT'
    app.state.billing.store.update('ticket','owner',item['id'],changed)
    response=client.post('/api/billing/email-approvals/'+approval['id']+'/send',json={'confirmed':True})
    assert response.status_code==409 and mail.sent==[]
    assert client.post('/api/billing/tickets/'+item['id']+'/inspect-portal').status_code==409
    foreign=app.state.billing.store.add('ticket','other-owner',{'billing_url':PAGE})
    assert client.post('/api/billing/tickets/'+foreign['id']+'/inspect-portal').status_code==404
