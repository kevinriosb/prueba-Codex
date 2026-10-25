import asyncio
import base64
import json
import io
import time
from email.message import EmailMessage
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from backend.app.billing.cfdi import SatVerifier
from backend.app.billing.extraction import DocumentExtractor
from backend.app.billing.documents import inspect_document
from backend.app.billing.providers import GmailProvider, SCOPES
from backend.app.billing.store import BillingStore
from backend.app.billing.telegram import TelegramClient
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.errors import AppError
from .billing_fakes import FISCAL, KEY, image, pdf, xml
from .conftest import TEST_PASSWORD
from .fake_model import ScriptedModel, answer
from .test_billing import billing_client, prepare, profile, ticket


def gmail_setup(tmp_path,handler):
    settings=Settings(_env_file=None,app_login_password=TEST_PASSWORD,billing_encryption_key=KEY,
        app_database_path=tmp_path/'gmail.sqlite3',app_allowed_hosts='testserver',
        gmail_client_id='client-demo',gmail_client_secret='client-secret-demo',
        gmail_redirect_uri='https://testserver/api/billing/gmail/callback')
    store=BillingStore(Database(settings.app_database_path),KEY)
    provider=GmailProvider(settings,store,client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return provider,store


async def test_gmail_oauth_pkce_scopes_encryption_and_replay(tmp_path):
    seen=[]
    def handler(request):
        seen.append(request.url.path)
        if request.url.path=='/token':
            body=parse_qs(request.content.decode())
            assert body['code_verifier']
            return httpx2.Response(200,json={'access_token':'access-demo','refresh_token':'refresh-demo',
                'scope':' '.join(SCOPES),'expires_in':3600})
        return httpx2.Response(200,json={'emailAddress':'mi-buzon@example.com'})
    provider,store=gmail_setup(tmp_path,handler)
    try:
        url=provider.connect('owner')
        query=parse_qs(urlsplit(url).query)
        assert query['code_challenge_method']==['S256'] and query['access_type']==['offline']
        assert set(query['scope'][0].split())==SCOPES
        assert 'client-secret-demo' not in url
        await provider.finish_connect(query['state'][0],'code-demo')
        assert provider.ready('owner') and not provider.ready('other-user')
        account=provider.account('owner')
        assert account['email']=='mi-buzon@example.com'
        with store.database.connection() as conn:
            payload=conn.execute("SELECT payload FROM billing_records WHERE kind='gmail'").fetchone()[0]
        assert b'refresh-demo' not in payload and b'mi-buzon@example.com' not in payload
        with pytest.raises(AppError):
            await provider.finish_connect(query['state'][0],'code-demo')
        assert seen.count('/token')==1
    finally:
        await provider.close()


async def test_gmail_refresh_send_and_reply_attachments_use_real_http_adapter(tmp_path):
    seen=[]
    def handler(request):
        seen.append((request.method,request.url.path))
        if request.url.path=='/token':
            assert parse_qs(request.content.decode())['grant_type']==['refresh_token']
            return httpx2.Response(200,json={'access_token':'renewed-demo','expires_in':3600})
        assert request.headers['authorization']=='Bearer renewed-demo'
        if request.url.path.endswith('/messages/send'):
            raw=json.loads(request.content)
            message=base64.urlsafe_b64decode(raw['raw'])
            assert b'To: facturacion@example.com' in message
            assert raw['threadId']=='thread_demo'
            return httpx2.Response(200,json={'id':'sent_demo','threadId':'thread_demo'})
        return httpx2.Response(200,json={'messages':[{'id':'reply_demo','payload':{
            'headers':[{'name':'From','value':'Facturación <facturacion@example.com>'},
                {'name':'In-Reply-To','value':'<request@agente.invalid>'}],
            'parts':[{'mimeType':'text/plain','body':{'data':base64.urlsafe_b64encode(b'Adjunto factura').decode()}},
                {'mimeType':'application/xml','filename':'factura.xml','body':{'data':base64.urlsafe_b64encode(xml()).decode()}}]}}]})
    provider,store=gmail_setup(tmp_path,handler)
    store.add('gmail','owner',{'email':'agente@example.com','refresh_token':'refresh-demo','access_token':'expired-demo','expires':0},lookup='account')
    try:
        message=EmailMessage()
        message['To']='facturacion@example.com'
        message['From']='agente@example.com'
        message.set_content('Prueba local con transporte simulado')
        delivery=await provider.send('owner',message,thread_id='thread_demo')
        assert delivery.message_id=='sent_demo'
        replies=await provider.fetch('owner',{'delivery':{'message_id':'sent_demo','thread_id':'thread_demo','rfc_message_id':'<request@agente.invalid>'}})
        assert len(replies)==1 and replies[0].sender=='facturacion@example.com'
        assert replies[0].attachments[0].data==xml()
        assert seen.count(('POST','/token'))==1
    finally:
        await provider.close()


async def test_sat_adapter_has_fixed_destination_and_disabled_default():
    requests=[]
    def handler(request):
        requests.append(request)
        assert request.url.host=='consultaqr.facturaelectronica.sat.gob.mx'
        assert b'&amp;' in request.content
        return httpx2.Response(200,content=b'<Envelope><CodigoEstatus>S - Comprobante obtenido satisfactoriamente.</CodigoEstatus><Estado>Vigente</Estado></Envelope>')
    verifier=SatVerifier(False,client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    data={'issuer_rfc':'BBB010101BBB','recipient_rfc':'AAA010101AAA','total':'116.00','uuid':'11111111-1111-4111-8111-111111111111'}
    try:
        with pytest.raises(AppError):
            await verifier.verify(data)
        assert requests==[]
        verifier.enabled=True
        result=await verifier.verify(data)
        assert result['vigente'] and result['query_success'] and len(requests)==1
    finally:
        await verifier.close()


async def test_gmail_fetch_accepts_long_attachment_ids_and_quotes_path_segments(tmp_path):
    long_identifier='A'*600+'+/=='
    def handler(request):
        if '/attachments/' in request.url.path:
            assert b'%2B%2F%3D%3D' in request.url.raw_path
            return httpx2.Response(200,json={'data':base64.urlsafe_b64encode(xml()).decode()})
        return httpx2.Response(200,json={'messages':[{'id':'reply_demo','payload':{
            'headers':[{'name':'From','value':'facturacion@example.com'},{'name':'In-Reply-To','value':'<request@agente.invalid>'}],
            'parts':[{'mimeType':'application/xml','filename':'factura.xml','body':{'size':len(xml()),'attachmentId':long_identifier}}]}}]})
    provider,store=gmail_setup(tmp_path,handler)
    store.add('gmail','owner',{'email':'agente@example.com','refresh_token':'refresh-demo',
        'access_token':'access-demo','expires':time.time()+3600},lookup='account')
    try:
        replies=await provider.fetch('owner',{'delivery':{'message_id':'sent_demo','thread_id':'thread_demo','rfc_message_id':'<request@agente.invalid>'}})
        assert len(replies)==1 and replies[0].attachments[0].data==xml()
    finally:
        await provider.close()


@pytest.mark.parametrize('mime,content,expected',[('image/jpeg',image(),'input_image'),('application/pdf',pdf(),'input_file')])
def test_actual_sdk_extracts_structured_document_without_network(billing_client,mime,content,expected):
    model=ScriptedModel([[answer(json.dumps(FISCAL))]])
    client,app,*_=billing_client(model=model)
    extractor=DocumentExtractor(app.state.service,app.state.settings)
    result=client.portal.call(extractor.extract,'csf',mime,content)
    assert result.rfc==FISCAL['rfc'] and len(model.inputs)==1
    assert model.inputs[0][0]['content'][1]['type']==expected
    assert app.state.billing.store.list('profile','owner')==[]


def test_double_confirmation_race_sends_once(billing_client):
    client,app,_,mail,*_=billing_client(billing_live_email_enabled=True)
    approval=prepare(client,ticket(client))
    async def race():
        mail.started,mail.release=asyncio.Event(),asyncio.Event()
        pending=asyncio.create_task(app.state.billing.send_invoice_request_email('owner',approval['id'],confirmed=True))
        await mail.started.wait()
        try:
            with pytest.raises(AppError) as failure:
                await app.state.billing.send_invoice_request_email('owner',approval['id'],confirmed=True)
            assert failure.value.status==409
        finally:
            mail.release.set()
        await pending
    client.portal.call(race)
    assert len(mail.sent)==1


def test_cannot_swap_encrypted_profile_between_owners(billing_client):
    client,app,*_=billing_client()
    selected=profile(client)
    with app.state.database.connection(True) as conn:
        conn.execute("UPDATE billing_records SET owner='other-user' WHERE id=?",(selected['id'],))
    with pytest.raises(AppError) as failure:
        app.state.billing.store.get('profile','other-user',selected['id'])
    assert failure.value.code=='billing_decryption'


def test_photo_orientation_is_preserved_when_metadata_is_removed():
    source=io.BytesIO()
    picture=Image.new('RGB',(32,16),'white')
    metadata=Image.Exif()
    metadata[274]=6
    metadata[315]='Metadato personal ficticio'
    picture.save(source,format='JPEG',exif=metadata)
    mime,data=inspect_document(source.getvalue(),8_388_608)
    assert mime=='image/jpeg'
    with Image.open(io.BytesIO(data)) as result:
        assert result.size==(16,32) and len(result.getexif())==0
    assert b'Metadato personal' not in data


def test_telegram_onboarding_consent_callbacks_and_owner_isolation(billing_client):
    client,app,extractor,mail,sat,telegram=billing_client(
        telegram_bot_token='token-demo',telegram_webhook_secret='x'*40,telegram_allowed_user_ids='123')
    client.headers.pop('origin')
    telegram.files['csf-demo']=image()
    count=0
    def update(message=None,data=None,user=123):
        nonlocal count
        count+=1
        body={'update_id':count}
        if data:
            body['callback_query']={'from':{'id':user},'message':{'chat':{'id':user,'type':'private'}},'data':data}
        else:
            body['message']={'from':{'id':user},'chat':{'id':user,'type':'private'},**message}
        response=client.post('/integrations/telegram/webhook',json=body,headers={'X-Telegram-Bot-Api-Secret-Token':'x'*40})
        assert response.status_code==200,response.text
        return body
    update({'text':'/start'})
    assert 'Constancia' in telegram.messages[-1][1]
    update({'document':{'file_id':'csf-demo'}})
    assert extractor.calls==[]
    analyze=telegram.messages[-1][2][0][1]
    submitted=update(data=analyze)
    assert len(extractor.calls)==1
    # Telegram vuelve a entregar el mismo evento: no se repite el análisis.
    assert client.post('/integrations/telegram/webhook',json=submitted,headers={'X-Telegram-Bot-Api-Secret-Token':'x'*40}).json()['duplicate'] is True
    update({'text':'G03'})
    update({'text':'cliente@example.com'})
    confirm=telegram.messages[-1][2][0][1]
    owner='tg_'+app.state.billing.store.digest('telegram:123')
    assert app.state.billing.store.list('profile',owner)==[]
    update(data=confirm)
    assert len(app.state.billing.store.list('profile',owner))==1
    assert app.state.billing.store.list('profile','owner')==[]
    update({'document':{'file_id':'csf-demo'}},user=999)
    assert telegram.downloads==['csf-demo'] and mail.sent==[]
    # La excepción del webhook no elimina CSRF/origen de otras rutas.
    assert client.post('/api/conversations').status_code==403


def test_telegram_rejects_invalid_webhook_secret(billing_client):
    client,app,_,_,_,telegram=billing_client(telegram_bot_token='token-demo',telegram_webhook_secret='x'*40,telegram_allowed_user_ids='123')
    assert client.post('/integrations/telegram/webhook',json={'update_id':1},headers={'X-Telegram-Bot-Api-Secret-Token':'wrong'}).status_code==403
    assert telegram.messages==[]


async def test_telegram_file_path_cannot_escape_fixed_host():
    seen=[]
    def handler(request):
        seen.append(request.url.path)
        return httpx2.Response(200,json={'ok':True,'result':{'file_path':'../secret','file_size':1}})
    settings=Settings(_env_file=None,app_login_password=TEST_PASSWORD,telegram_bot_token='token-demo')
    client=TelegramClient(settings,client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    try:
        with pytest.raises(AppError) as failure:
            await client.download('file-demo')
        assert failure.value.code=='telegram_file' and len(seen)==1
    finally:
        await client.close()
