import json
import asyncio
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from backend.app.billing.extraction import FieldExtraction
from backend.app.billing.portal_inspection import field_key
from backend.app.errors import AppError
from backend.browser_worker.engine import BrowserManager
from backend.browser_worker.policy import BrowserError,PublicFetcher
from .test_browser_worker import Pages,HOME,ALLOWED
from .test_portal_preparation import ScopedExtractor,prepared
from .billing_fakes import image,xml,pdf
from .test_telegram_agent import button,owner
from .test_billing import billing_client


FORM='''<form action="/lookup" method="post"><label>RFC<input name="rfc" required></label>
 <label>Ticket<input name="ticket" required></label><button>Buscar ticket</button></form>'''
FISCAL='''<form action="/issue" method="post"><label>Nombre fiscal<input name="name" required></label>
 <label>CP fiscal<input name="cp" required></label><label>Uso CFDI<select name="use"><option value="G03">G03 - Gastos en general</option></select></label>
 <button>Generar factura</button></form>'''


class PortalPages(Pages):
    def __init__(self,form=FORM):
        super().__init__({HOME:form})
        self.sent=[]
        self.responses={'/lookup':FISCAL,'/issue':'<p>Factura generada</p><a href="/invoice.xml">XML</a><a href="/invoice.pdf">PDF</a>'}
        self.fail=False
        self.invoice_xml=b'<fake>http://www.sat.gob.mx/cfd/</fake>'
        self.invoice_pdf=b'%PDF-TEST'

    async def request_limited(self,method,url,headers,body=None,*,on_bytes=None):
        self.sent.append((method,url,headers,body))
        if self.fail:raise TimeoutError()
        if url.endswith('.xml'):return 200,{'content-type':'application/xml'},self.invoice_xml
        if url.endswith('.pdf'):return 200,{'content-type':'application/pdf'},self.invoice_pdf
        path=urlsplit(url).path
        return 200,{'content-type':'text/html; charset=utf-8'},self.responses.get(path,'<p>Respuesta desconocida</p>').encode()


def submission_args(view,values,*,operation='11111111-1111-4111-8111-111111111111'):
    action=next(a for a in view['actions'] if a['kind']!='download')
    return dict(operation_id=operation,observation_id=view['snapshot']['observation_id'],reference=action['reference'],
        origins=[action['origin']],values=[{'reference':f['reference'],'value':v} for f,v in zip(view['snapshot']['fields'],values)])


async def confirmed_send(manager,user,sid,**args):
    """Dos decisiones explícitas de la prueba: llenar offline y luego enviar."""
    session=manager.sessions.get(sid)
    if session and args['operation_id'] in session.transaction.get('operations',{}):
        return await manager.transaction_send(user,sid,**args)
    import uuid
    prepared=await manager.transaction_send(user,sid,**(args|{'operation_id':str(uuid.uuid4()),'prepare_only':True}))
    if prepared['state']!='prepared':return prepared
    return await manager.transaction_send(user,sid,**args)


async def test_two_different_forms_advance_only_after_each_native_approval_and_capture_invoice_files():
    pages=PortalPages();manager=BrowserManager(fetcher=pages)
    try:
        opened=await manager.open('owner',HOME,ALLOWED)
        sid=opened['session_id']
        view=await manager.transaction_view('owner',sid)
        assert [a['kind'] for a in view['actions']]==['continue'] and pages.sent==[]
        args=submission_args(view,['AAA010101AAA','000100'])
        first=await confirmed_send(manager,'owner',sid,**args)
        assert first['state']=='review',first
        assert len(pages.sent)==1 and pages.sent[0][0]=='POST'
        assert b'ticket=000100' in pages.sent[0][3]
        assert all('value' not in f for f in first['view']['snapshot']['fields'])
        assert not first['view']['snapshot']['public_only']
        assert (await confirmed_send(manager,'owner',sid,**args))==first
        assert len(pages.sent)==1
        with pytest.raises(BrowserError,match='browser_private'):await manager.observe('owner',sid)
        second=await confirmed_send(manager,'owner',sid,**submission_args(first['view'],['PERSONA PRUEBA','11520','G03'],operation='22222222-2222-4222-8222-222222222222'))
        assert second['state']=='submitted',second
        assert {f['mime'] for f in second['view']['files']}=={'application/xml','application/pdf'}
        file=second['view']['files'][0]
        content=await manager.transaction_file('owner',sid,file['reference'])
        assert content['data'] and 'data' not in json.dumps(second)
        with pytest.raises(BrowserError):await manager.transaction_file('other',sid,file['reference'])
    finally:await manager.close()


async def test_timeout_after_dispatch_is_unknown_and_does_not_resubmit_for_another_operation():
    pages=PortalPages();manager=BrowserManager(fetcher=pages)
    try:
        opened=await manager.open('owner',HOME,ALLOWED)
        view=await manager.transaction_view('owner',opened['session_id'])
        pages.fail=True
        args=submission_args(view,['AAA010101AAA','000100'])
        result=await confirmed_send(manager,'owner',opened['session_id'],**args)
        assert result['state']=='unknown' and len(pages.sent)==1
        assert (await confirmed_send(manager,'owner',opened['session_id'],**args))['state']=='unknown'
        with pytest.raises(BrowserError):await confirmed_send(manager,'owner',opened['session_id'],**(args|{'operation_id':'33333333-3333-4333-8333-333333333333'}))
        assert len(pages.sent)==1
    finally:await manager.close()


async def test_changed_form_target_foreign_owner_and_missing_values_never_send():
    pages=PortalPages();manager=BrowserManager(fetcher=pages)
    try:
        opened=await manager.open('owner',HOME,ALLOWED)
        sid=opened['session_id'];view=await manager.transaction_view('owner',sid)
        args=submission_args(view,['AAA010101AAA','000100'])
        with pytest.raises(BrowserError):await confirmed_send(manager,'other',sid,**args)
        session=await manager.get('owner',sid)
        await session.page.locator('form').evaluate("e=>e.action='https://other.example/issue'")
        with pytest.raises(BrowserError,match='browser_stale'):await confirmed_send(manager,'owner',sid,**args)
        assert pages.sent==[]
        await session.page.locator('form').evaluate("e=>e.action='/lookup'")
        view=await manager.transaction_view('owner',sid)
        result=await confirmed_send(manager,'owner',sid,**submission_args(view,[]))
        assert result['state']=='correction' and pages.sent==[]
    finally:await manager.close()


async def test_json_form_with_server_validation_can_be_corrected_without_generic_retry():
    form='''<label>Cuenta<input id="account" required></label><button type="button" onclick="fetch('/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cuenta:document.querySelector('input').value})}).then(()=>{document.body.innerHTML='<div role=alert>Cuenta incorrecta</div><label>Cuenta<input required></label><button>Continuar</button>';})">Validar</button>'''
    pages=PortalPages(form);manager=BrowserManager(fetcher=pages)
    try:
        opened=await manager.open('owner',HOME,ALLOWED)
        view=await manager.transaction_view('owner',opened['session_id'])
        result=await confirmed_send(manager,'owner',opened['session_id'],**submission_args(view,['00123']))
        assert result['state']=='correction' and 'incorrecta' in result['view']['error']
        assert json.loads(pages.sent[0][3])=={'cuenta':'00123'}
        assert pages.sent[0][2]['content-type']=='application/json'
    finally:await manager.close()


def enable_transactions(browser,pages):
    browser.manager.fetcher=pages
    async def view(owner,sid):
        try:result=await browser.manager.transaction_view(owner,sid)
        except BrowserError as error:raise AppError(error.status,error.code,error.message) from None
        for f in result['snapshot']['fields']:f['key']=field_key(f['label'])
        return result
    async def send(owner,sid,**payload):
        try:result=await browser.manager.transaction_send(owner,sid,**payload)
        except BrowserError as error:raise AppError(error.status,error.code,error.message) from None
        for f in result['view']['snapshot']['fields']:f['key']=field_key(f['label'])
        return result
    async def status(owner,sid,op):
        try:return await browser.manager.transaction_status(owner,sid,op)
        except BrowserError as error:raise AppError(error.status,error.code,error.message) from None
    browser.transaction_view=view
    browser.transaction_send=send
    browser.transaction_status=status
    browser.transaction_file=browser.manager.transaction_file


def test_telegram_native_consent_does_not_send_on_preview_and_profile_change_invalidates_approval(billing_client):
    values,browser,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    pages=PortalPages();enable_transactions(browser,pages)
    chat(data='lv:'+app.state.billing.store.list('ticket',owner(app))[0]['id'])
    approval=button(telegram,'ls')
    assert pages.sent==[] and mail.sent==[]
    chat(data=approval,user=456)
    assert pages.sent==[]
    profile=app.state.billing.store.list('profile',owner(app))[0]
    app.state.billing.store.update('profile',owner(app),profile['id'],profile|{'email':'changed@example.com'})
    chat(data=approval)
    assert pages.sent==[] and 'Cambiaron' in telegram.messages[-1][1]


def test_crop_is_scoped_to_one_field_requires_consent_and_manual_text_stays_in_same_request(billing_client):
    class Crops(ScopedExtractor):
        async def extract_field(self,mime,data,label,key):
            self.calls.append(('crop',label))
            return FieldExtraction(value='00042')
    values,browser,model=prepared(billing_client,extractor=Crops())
    client,app,extractor,mail,sat,telegram,chat=values
    enable_transactions(browser,PortalPages())
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='lv:'+ticket['id'])
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    field=next(f for f in tx['view']['snapshot']['fields'] if f['key']=='store_number')
    chat(data='lm:'+tx['id']+':'+field['reference'])
    telegram.files['crop']=image('blue')
    chat(document='crop')
    assert not any(c[0]=='crop' for c in extractor.calls)
    chat(data=button(telegram,'la'))
    assert telegram.messages[-1][1]=='Leí Tienda: 00042'
    chat(data=button(telegram,'lu'))
    tx=app.state.billing.portal_submission.current(owner(app),ticket['id'])
    assert list(tx['manual'].values())==['00042']
    assert len(app.state.billing.store.list('ticket',owner(app)))==1
    chat(data='lm:'+tx['id']+':'+field['reference'])
    chat('00043')
    assert '00043' in telegram.messages[-1][1] and mail.sent==[]
    assert len(app.state.billing.store.list('ticket',owner(app)))==1


def test_telegram_requests_real_portal_step_then_keeps_xml_pdf_validates_and_never_sends_twice(billing_client):
    values,browser,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    pages=PortalPages();pages.invoice_xml=xml();pages.invoice_pdf=pdf()
    enable_transactions(browser,pages)
    item=app.state.billing.store.list('ticket',owner(app))[0]
    chat('/estado')
    assert any(data=='lv:'+item['id'] for _,data in telegram.messages[-1][2])
    chat(data='lv:'+item['id'])
    approval=button(telegram,'ls')
    assert pages.sent==mail.sent==sat.calls==[]
    chat(data=approval)
    assert pages.sent==[]
    chat(data=button(telegram,'lc'))
    result=app.state.billing.resolve_ticket(owner(app),item['id'])
    assert result['portal_delivery']['state']=='submitted'
    assert result['validation']['structural_match'] and not result['validation']['sat_verified']
    assert len(result['invoice_documents'])==2
    assert 'XML recibido' in telegram.messages[-1][1]
    calls=len(pages.sent)
    chat(data=approval)
    chat(data='lq:'+item['id'])
    assert len(pages.sent)==calls and len(app.state.billing.resolve_ticket(owner(app),item['id'])['invoice_documents'])==2
    chat(data='sv:'+item['id'])
    assert len(sat.calls)==1
    chat(data='lq:'+item['id'])
    result=app.state.billing.resolve_ticket(owner(app),item['id'])
    assert result['status']=='FACTURACION_COMPLETADA' and result['validation']['sat_verified']
    assert mail.sent==[]
    with pytest.raises(AppError):app.state.billing.prepare_invoice_request_email(owner(app),item['id'])


def test_invalid_xml_never_completes_a_bill_or_retries_the_portal(billing_client):
    values,browser,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    pages=PortalPages();pages.invoice_xml=xml(rfc='ZZZ010101ZZZ');pages.invoice_pdf=pdf()
    enable_transactions(browser,pages)
    item=app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='lv:'+item['id']);chat(data=button(telegram,'ls'));chat(data=button(telegram,'lc'))
    result=app.state.billing.resolve_ticket(owner(app),item['id'])
    assert result['portal_delivery']['state']=='submitted' and not result.get('validation')
    assert result['invoice_documents']==[] and 'no corresponde' in telegram.messages[-1][1]
    calls=len(pages.sent);chat(data='lq:'+item['id'])
    assert len(pages.sent)==calls


def test_new_required_field_is_requested_in_telegram_after_a_nonfinal_postback(billing_client):
    values,browser,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    pages=PortalPages()
    pages.responses['/issue']='''<form action="/finish" method="post"><label>Número exterior<input name="number" required></label><button>Emitir factura</button></form>'''
    pages.responses['/finish']='<p>Factura generada</p>'
    enable_transactions(browser,pages)
    item=app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='lv:'+item['id']);chat(data=button(telegram,'ls'));chat(data=button(telegram,'lc'))
    assert 'Falta: Número exterior' in telegram.messages[-1][1]
    tx=app.state.billing.portal_submission.current(owner(app),item['id'])
    assert tx['state']=='review' and len(pages.sent)==1
    field=tx['view']['snapshot']['fields'][0]
    chat(data='lm:'+tx['id']+':'+field['reference']);chat('0014')
    assert 'Número exterior: 0014' in telegram.messages[-1][1]
    chat(data=button(telegram,'ls'));chat(data=button(telegram,'lc'))
    assert b'number=0014' in pages.sent[-1][3]
    assert 'Solicitud enviada' in telegram.messages[-1][1]


def test_worker_api_authentication_protocol_and_owner_isolation(tmp_path):
    from fastapi.testclient import TestClient
    from backend.browser_worker.app import create_worker
    from backend.app.billing.browser import TransactionView,TransactionResult
    token=tmp_path/'token';token.write_text('t'*40)
    pages=PortalPages();manager=BrowserManager(fetcher=pages)
    with TestClient(create_worker(manager,token)) as client:
        headers={'X-Browser-Key':'t'*40}
        sid=client.post('/v1/sessions',headers=headers,json={'owner':'owner','url':HOME,'allowed':ALLOWED}).json()['session_id']
        url='/v1/sessions/'+sid+'/transaction'
        assert client.get(url,params={'owner':'owner'}).status_code==403
        assert client.get(url,headers=headers,params={'owner':'other'}).status_code==404
        view=TransactionView.model_validate(client.get(url,headers=headers,params={'owner':'owner'}).json()).model_dump(mode='json')
        payload={'owner':'owner',**submission_args(view,['AAA010101AAA','000100'])}
        assert client.post(url,headers=headers,json=payload|{'observation_id':'bad'}).status_code==422
        assert pages.sent==[]
        import uuid
        prepared=client.post(url,headers=headers,json=payload|{'prepare_only':True,'operation_id':str(uuid.uuid4())})
        assert prepared.json()['state']=='prepared' and pages.sent==[]
        result=client.post(url,headers=headers,json=payload)
        parsed=TransactionResult.model_validate(result.json())
        assert parsed.state=='review'
        calls=len(pages.sent)
        assert client.get(url+'/'+payload['operation_id'],headers=headers,params={'owner':'owner'}).json()==result.json()
        assert len(pages.sent)==calls


async def test_post_transport_pins_ip_and_tls_and_preserves_body_content_type_without_following_redirects():
    import httpx2
    seen=[]
    async def resolver(host):return ['93.184.216.34']
    def handler(request):
        seen.append(request)
        return httpx2.Response(303,headers={'location':'https://other.example/next'},content=b'')
    client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    fetcher=PublicFetcher(resolver=resolver,client=client)
    try:
        result=await fetcher.request_limited('POST','https://merchant.example/issue',
            {'content-type':'application/x-www-form-urlencoded','cookie':'csrf=test','x-xsrf-token':'nonce-demo',
                'authorization':'Bearer portal-demo'},b'ticket=0012&rfc=AAA010101AAA')
        assert result[0]==303 and len(seen)==1
        assert seen[0].method=='POST' and seen[0].url.host=='93.184.216.34'
        assert seen[0].headers['host']=='merchant.example' and seen[0].extensions['sni_hostname']=='merchant.example'
        assert seen[0].headers['content-type']=='application/x-www-form-urlencoded'
        assert seen[0].headers['x-xsrf-token']=='nonce-demo' and seen[0].headers['authorization']=='Bearer portal-demo'
        assert seen[0].content==b'ticket=0012&rfc=AAA010101AAA'
    finally:await fetcher.close()


async def test_input_script_cannot_replace_the_authorized_submit_action():
    form='''<form action="/lookup" method="post"><label>Cuenta<input name="account" required oninput="this.form.action='/delete'"></label><button>Continuar</button></form>'''
    pages=PortalPages(form);manager=BrowserManager(fetcher=pages)
    try:
        opened=await manager.open('owner',HOME,ALLOWED)
        view=await manager.transaction_view('owner',opened['session_id'])
        result=await confirmed_send(manager,'owner',opened['session_id'],**submission_args(view,['0004']))
        assert result['state']=='correction' and pages.sent==[]
    finally:await manager.close()


def test_lost_internal_response_is_recovered_by_status_without_a_second_portal_submission(billing_client):
    values,browser,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    pages=PortalPages();pages.invoice_xml=xml();pages.invoice_pdf=pdf()
    enable_transactions(browser,pages)
    original=browser.transaction_send
    async def lose_response(*args,**kwargs):
        result=await original(*args,**kwargs)
        if not kwargs.get('prepare_only'):raise AppError(502,'portal_send_unknown','Se perdió la respuesta interna.')
        return result
    browser.transaction_send=lose_response
    item=app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='lv:'+item['id']);chat(data=button(telegram,'ls'));chat(data=button(telegram,'lc'))
    assert 'sin confirmar' in telegram.messages[-1][1]
    assert app.state.billing.portal_submission.current(owner(app),item['id'])['state']=='unknown'
    count=len(pages.sent)
    with pytest.raises(AppError):app.state.billing.prepare_invoice_request_email(owner(app),item['id'])
    chat(data='lq:'+item['id'])
    assert 'XML recibido' in telegram.messages[-1][1]
    assert len(pages.sent)==count and len(app.state.billing.resolve_ticket(owner(app),item['id'])['invoice_documents'])==2


def test_unreadable_crop_remains_empty_and_accepts_manual_entry_without_another_receipt(billing_client):
    class Crops(ScopedExtractor):
        async def extract_field(self,*args):return FieldExtraction(value=None)
    values,browser,model=prepared(billing_client,extractor=Crops())
    client,app,extractor,mail,sat,telegram,chat=values
    enable_transactions(browser,PortalPages())
    item=app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='lv:'+item['id'])
    tx=app.state.billing.portal_submission.current(owner(app),item['id'])
    field=next(f for f in tx['view']['snapshot']['fields'] if f['key']=='store_number')
    chat(data='lm:'+tx['id']+':'+field['reference'])
    telegram.files['crop']=image();chat(document='crop');chat(data=button(telegram,'la'))
    assert 'No se lee ese campo' in telegram.messages[-1][1]
    assert app.state.billing.portal_submission.current(owner(app),item['id'])['manual']=={}
    chat('00115')
    assert app.state.billing.resolve_ticket(owner(app),item['id'])['store_number']=='00115'
    assert len(app.state.billing.store.list('ticket',owner(app)))==1


def test_real_agent_browser_client_uses_the_private_protocol_and_keeps_public_calls_closed(tmp_path):
    from fastapi.testclient import TestClient
    from backend.browser_worker.app import create_worker
    from backend.app.billing.browser import PortalBrowserClient
    token=tmp_path/'token';token.write_text('t'*40)
    pages=PortalPages();manager=BrowserManager(fetcher=pages)
    with TestClient(create_worker(manager,token)) as worker:
        class Transport:
            async def request(self,method,url,**kwargs):
                kwargs.pop('timeout',None)
                return worker.request(method,url,**kwargs)
        browser=PortalBrowserClient(SimpleNamespace(billing_browser_enabled=True,billing_browser_token_file=token,
            billing_browser_worker_url='http://testserver'),client=Transport())
        async def scenario():
            opened=await browser.open('owner',HOME,allowed=ALLOWED)
            sid=opened['session_id']
            view=await browser.transaction_view('owner',sid)
            args=submission_args(view,['AAA010101AAA','00115'])
            import uuid
            prepared=await browser.transaction_send('owner',sid,**(args|{'prepare_only':True,'operation_id':str(uuid.uuid4())}))
            assert prepared['state']=='prepared' and pages.sent==[]
            result=await browser.transaction_send('owner',sid,**args)
            assert result['state']=='review'
            assert all('key' in f for f in result['view']['snapshot']['fields'])
            with pytest.raises(AppError) as private:await browser.observe('owner',sid)
            assert private.value.code=='browser_private'
            read=await browser.transaction_status('owner',sid,result['operation_id'])
            assert read==result and len(pages.sent)==1
        asyncio.run(scenario())


async def test_xml_validation_response_is_not_mistaken_for_a_cfdi():
    form='''<label>Cuenta<input required></label><button type="button" onclick="fetch('/check',{method:'POST',body:'test'}).then(r=>r.text()).then(()=>{document.querySelector('div').textContent='Cuenta incorrecta';})">Validar</button><div role="alert"></div>'''
    class Validation(PortalPages):
        async def request_limited(self,*args,**kwargs):
            self.sent.append(args)
            return 200,{'content-type':'application/xml'},b'<error>Cuenta incorrecta</error>'
    pages=Validation(form);manager=BrowserManager(fetcher=pages)
    try:
        opened=await manager.open('owner',HOME,ALLOWED)
        view=await manager.transaction_view('owner',opened['session_id'])
        result=await confirmed_send(manager,'owner',opened['session_id'],**submission_args(view,['0004']))
        assert result['state']=='correction' and result['view']['files']==[]
    finally:await manager.close()


async def test_invoice_evidence_prevents_retry_even_when_old_validation_alert_remains():
    form='''<label>Cuenta<input required></label><div role=alert>Error anterior</div>
     <button type=button onclick="fetch('/invoice.xml',{method:'POST',body:'cuenta=4'}).then(r=>r.text())">Generar factura</button>'''
    pages=PortalPages(form);pages.invoice_xml=xml()
    manager=BrowserManager(fetcher=pages)
    try:
        opened=await manager.open('owner',HOME,ALLOWED)
        view=await manager.transaction_view('owner',opened['session_id'])
        result=await confirmed_send(manager,'owner',opened['session_id'],**submission_args(view,['0004']))
        assert result['state']=='submitted' and result['view']['files'] and result['view']['error']
        calls=len(pages.sent)
        with pytest.raises(BrowserError):await confirmed_send(manager,'owner',opened['session_id'],**submission_args(result['view'],['0005'],operation='33333333-3333-4333-8333-333333333333'))
        assert len(pages.sent)==calls
    finally:await manager.close()


def test_old_crop_consent_cannot_spend_tokens_after_the_step_was_sent(billing_client):
    class Crops(ScopedExtractor):
        async def extract_field(self,*args):
            raise AssertionError('Una foto antigua no debe llegar a IA.')
    values,browser,model=prepared(billing_client,extractor=Crops())
    client,app,extractor,mail,sat,telegram,chat=values
    enable_transactions(browser,PortalPages())
    item=app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='lv:'+item['id'])
    tx=app.state.billing.portal_submission.current(owner(app),item['id'])
    field=next(f for f in tx['view']['snapshot']['fields'] if f['key']=='store_number')
    chat(data='lm:'+tx['id']+':'+field['reference']);telegram.files['crop']=image();chat(document='crop')
    consent=button(telegram,'la')
    app.state.billing.store.update('portal_transaction',owner(app),tx['id'],tx|{'state':'submitted'})
    chat(data=consent)
    assert 'foto no se analizará' in telegram.messages[-1][1]


def test_expired_public_preview_can_be_recreated_without_duplicate_transaction_records(billing_client):
    values,browser,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    pages=PortalPages();enable_transactions(browser,pages)
    item=app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='lv:'+item['id'])
    tx=app.state.billing.portal_submission.current(owner(app),item['id'])
    view=browser.transaction_view
    expired=False
    async def expire_once(*args):
        nonlocal expired
        if not expired:
            expired=True
            await browser.manager.close_session(*args)
            raise AppError(404,'browser_session','Venció.')
        return await view(*args)
    browser.transaction_view=expire_once
    chat(data='lr:'+item['id'])
    new=app.state.billing.portal_submission.current(owner(app),item['id'])
    assert new['id']==tx['id'] and new['session_id']!=tx['session_id']
    assert len(app.state.billing.store.list('portal_transaction',owner(app)))==1
    assert pages.sent==[]


def test_session_missing_before_dispatch_is_expired_not_an_uncertain_invoice(billing_client):
    values,browser,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    pages=PortalPages();enable_transactions(browser,pages)
    async def gone(*args,**kwargs):raise AppError(404,'browser_session','La sesión venció antes del envío.')
    browser.transaction_send=gone
    item=app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='lv:'+item['id']);chat(data=button(telegram,'ls'))
    tx=app.state.billing.portal_submission.current(owner(app),item['id'])
    assert tx['state']=='expired' and not tx['private'] and pages.sent==[]


async def test_private_step_never_sends_field_values_to_a_new_cdn_script_url():
    class Assets(PortalPages):
        async def fetch(self,url,headers):
            if url.startswith('https://cdn.example/'):
                self.calls.append((url,headers))
                return 200,{'content-type':'application/javascript'},b'window.assetReady=true;'
            return await super().fetch(url,headers)
        async def request_limited(self,method,url,headers,body=None,**kwargs):
            if url=='https://cdn.example/library.js':
                self.sent.append((method,url,headers,body))
                return 200,{'content-type':'application/javascript'},b'window.assetReady=true;'
            return await super().request_limited(method,url,headers,body,**kwargs)
    pages=Assets('<script src="https://cdn.example/library.js"></script>'+FORM)
    pages.responses['/lookup']=FISCAL+'''<script src="https://cdn.example/library.js"></script><script src="https://cdn.example/library.js?rfc=PRIVATE"></script>'''
    manager=BrowserManager(fetcher=pages)
    try:
        opened=await manager.open('owner',HOME,ALLOWED,public_route=True)
        view=await manager.transaction_view('owner',opened['session_id'])
        result=await confirmed_send(manager,'owner',opened['session_id'],**submission_args(view,['AAA010101AAA','0004']))
        assert not any('PRIVATE' in url for _,url,_,_ in pages.sent)
        asset=next(call for call in pages.sent if call[1]=='https://cdn.example/library.js')
        assert 'cookie' not in asset[2] and 'referer' not in asset[2]
        assert result['view']['snapshot']['partial']
    finally:await manager.close()
