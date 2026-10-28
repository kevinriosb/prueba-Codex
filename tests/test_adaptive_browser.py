import copy
import json
import uuid

import httpx2
import pytest

from backend.app.billing.browser import PortalBrowserClient
from backend.app.billing.portal_inspection import field_key
from backend.app.errors import AppError
from scripts.configure_browser import domains
from .billing_fakes import FISCAL,image
from .fake_model import ScriptedModel,answer,call
from .test_billing import billing_client,profile,ticket
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile


def snapshot(*,fields=None,actions=None,observed='aaaaaaaa'):
    return {'session_id':'11111111-1111-4111-8111-111111111111','observation_id':observed,
        'page':'https://merchant.example/','title':'Portal público de prueba','fields':fields or [],
        'actions':actions or [],'captcha':False,'blocked_origins':[],'network_blocked':False,
        'steps_remaining':6,'public_only':True,'purchase_validated':False,'submitted':False}


class FakeBrowser:
    enabled=True
    def __init__(self):
        self.calls=[]
    async def open(self,owner,url,*,allowed=None,public_route=False):
        self.calls.append(('open',owner,url))
        return snapshot(actions=[{'reference':'bbbbbbbb','label':'Facturación','kind':'link'}])
    async def act(self,owner,session,observed,reference):
        self.calls.append(('act',owner,session,observed,reference))
        return snapshot(fields=[{'key':'account_number','label':'Cuenta','required':True,'kind':'input','options':[]}],
            observed='cccccccc')
    async def observe(self,owner,session):
        self.calls.append(('observe',owner,session))
        return snapshot(actions=[{'reference':'eeeeeeee','label':'Facturación','kind':'link'}],observed='dddddddd')
    async def allow(self,owner,session,allowed,*,public_route=False):
        self.calls.append(("allow",owner,session,allowed))
        return snapshot(actions=[{"reference":"eeeeeeee","label":"Facturación","kind":"link"}],observed="dddddddd")
    async def close_session(self,owner,session):
        self.calls.append(("close",owner,session))
    async def close(self): pass


def grant(client,identifier,*,origins=None,intent='browse'):
    response=client.post('/api/billing/tickets/'+identifier+'/portal-permissions',json={'origins':origins,'intent':intent})
    assert response.status_code==200,response.json()
    result=client.post('/api/billing/portal-permissions/'+response.json()['id']+'/decision',json={'approve':True})
    assert result.status_code==200,result.json()


def test_real_agents_sdk_observes_and_chooses_public_link_without_fiscal_values(billing_client):
    browser=FakeBrowser()
    model=ScriptedModel([[call('portal_browser_observe')],
        [call('portal_browser_navigate',observation_id='aaaaaaaa',reference='bbbbbbbb')],
        [answer('Revisa el campo Cuenta.')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser)
    confirm_profile(telegram,chat)
    extractor.ticket['billing_url']='https://merchant.example/'
    telegram.files['receipt']=image('red')
    chat(document='receipt');chat(data=button(telegram,'an'))
    before=len(telegram.messages)
    chat(data=button(telegram,'wa'))
    assert [item[0] for item in browser.calls]==['open','act']
    assert len(telegram.messages)==before+1  # Solo la observación más reciente.
    assert 'Cuenta: Pendiente' in telegram.messages[-1][1]
    assert 'No se solicitó la factura' in telegram.messages[-1][1]
    assert all(value not in json.dumps(model.inputs) for value in (FISCAL['rfc'],FISCAL['name'],'cliente@example.com'))
    assert mail.sent==sat.calls==[] and len(extractor.calls)==2


def test_browser_api_is_owned_csrf_protected_and_does_not_take_arbitrary_operations(billing_client):
    browser=FakeBrowser()
    client,app,extractor,*_=billing_client(browser=browser)
    extractor.ticket['billing_url']='https://merchant.example/'
    item=ticket(client)
    path='/api/billing/tickets/'+item['id']+'/browser'
    assert client.post(path,headers={'X-CSRF-Token':'wrong'}).status_code==403
    assert client.post(path).status_code==403 and browser.calls==[]
    grant(client,item['id'])
    result=client.post(path)
    assert result.status_code==200 and len(browser.calls)==1
    stale=client.post(path+'/actions',json={'observation_id':'aaaaaaaa','reference':'dddddddd'})
    assert stale.status_code==409 and len(browser.calls)==1
    assert client.post(path+'/actions',json={'observation_id':'aaaaaaaa','reference':'bbbbbbbb','script':'arbitrary code'}).status_code==422
    assert client.post('/api/billing/tickets/'+str(uuid.uuid4())+'/browser').status_code==404
    refreshed=client.post(path).json()['snapshot']
    assert refreshed['observation_id']=='dddddddd' and browser.calls[-1][0]=='observe'


def test_browser_controls_cannot_open_another_telegram_users_ticket(billing_client):
    browser=FakeBrowser()
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,browser=browser)
    confirm_profile(telegram,chat)
    extractor.ticket['billing_url']='https://merchant.example/'
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    chat(data=button(telegram,'wa'),user=456)
    assert browser.calls==[] and 'no encontrado' in telegram.messages[-1][1]


def test_navigation_disabled_by_default_without_spawning_browser_or_charging_ai(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    confirm_profile(telegram,chat)
    extractor.ticket['billing_url']='https://merchant.example/'
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    item=app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='bn:'+item['id'])
    assert 'no está activada' in telegram.messages[-1][1]
    assert app.state.database.usage(owner(app))['runs']==0
    assert not any(tool.name.startswith('portal_browser_') for tool in app.state.service.tools)


def test_browser_domain_failure_is_shown_without_an_expensive_retry_loop(billing_client):
    class Denied(FakeBrowser):
        async def open(self,owner,url,*,allowed=None,public_route=False):
            raise AppError(403,'browser_origin','Ese dominio requiere autorización del operador.')
    model=ScriptedModel([[call('portal_browser_observe')],[answer('Revisa el dominio.')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=Denied())
    confirm_profile(telegram,chat)
    extractor.ticket['billing_url']='https://merchant.example/'
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    chat(data=button(telegram,'wa'))
    assert 'requiere autorización' in telegram.messages[-1][1]
    assert all(not value.startswith('bn:') for _,value in telegram.messages[-1][2])
    assert len(model.inputs)==2 and len(extractor.calls)==2


def test_discovered_optional_alternative_can_be_completed_without_making_it_universally_required(billing_client):
    from backend.app.billing.receipt import pending_receipt_fields
    client,app,extractor,*_=billing_client(browser=FakeBrowser())
    item=ticket(client)
    record=app.state.billing.store.get('ticket','owner',item['id'])
    record['portal_browser']={'fields':[{'key':'lane','label':'Carril','required':False}]}
    app.state.billing.store.update('ticket','owner',item['id'],record)
    assert 'lane' not in pending_receipt_fields(record)
    result=app.state.billing.complete_ticket_field('owner',item['id'],'lane','DEMO-2')
    assert result['lane']=='DEMO-2'
    untouched=app.state.billing.store.get('profile','owner',item['profile_id'])
    assert untouched['rfc']==FISCAL['rfc']


def test_public_select_options_do_not_expand_model_context_or_token_cost(billing_client):
    class VerboseBrowser(FakeBrowser):
        async def open(self,owner,url,*,allowed=None,public_route=False):
            result=snapshot(fields=[{'key':'cfdi_use','label':'Uso CFDI '+str(index),'required':False,
                'kind':'select','options':['option-not-for-model-'+str(option) for option in range(25)]}
                for index in range(24)])
            result['page']='https://merchant.example/receipt-path-not-for-model'
            return result
    model=ScriptedModel([[call('portal_browser_observe')],[answer('Revisa los campos visibles.')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=VerboseBrowser())
    confirm_profile(telegram,chat)
    extractor.ticket['billing_url']='https://merchant.example/'
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    chat(data=button(telegram,'wa'))
    serialized=json.dumps(model.inputs,ensure_ascii=False)
    assert 'option-not-for-model-' not in serialized
    assert 'receipt-path-not-for-model' not in serialized
    assert len(model.inputs)==1
    with app.state.database.connection() as connection:
        row=connection.execute("SELECT history FROM conversations WHERE user_id=? AND purpose='portal_review'",(owner(app),)).fetchone()
    outputs=[item for item in json.loads(row['history']) if item.get('type')=='function_call_output']
    assert len(outputs[-1]['output'].encode())<4000
    assert mail.sent==[]


@pytest.mark.parametrize('public_route',[False,True])
async def test_worker_client_sends_no_purchase_or_fiscal_values_and_validates_schema(tmp_path,public_route):
    from backend.app.config import Settings
    token=tmp_path/'token';token.write_text('fake-private-browser-key-'+'x'*40)
    requests=[]
    def transport(request):
        requests.append(request)
        return httpx2.Response(200,json=snapshot(fields=[{'label':'Web ID','required':False,'kind':'input','options':[]}]))
    settings=Settings(_env_file=None,app_login_password='test-only-password-123',
        billing_browser_enabled=True,billing_browser_token_file=token)
    browser=PortalBrowserClient(settings,client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)))
    try:
        result=await browser.open('opaque-owner','http://merchant.example/',allowed=['https://merchant.example'],public_route=public_route)
        assert result['fields'][0]['key']=='web_id'
        assert json.loads(requests[0].content)=={'owner':'opaque-owner','url':'https://merchant.example/',
            'allowed':['https://merchant.example'],**({'public_route':True} if public_route else {})}
        assert requests[0].headers['x-browser-key']==token.read_text()
        assert FISCAL['rfc'] not in requests[0].content.decode()
        await browser.open('opaque-owner','https://merchant.example/?ticket=not-to-forward#secret-fragment',allowed=['https://merchant.example'])
        assert json.loads(requests[-1].content)['url']=='https://merchant.example/'
        with pytest.raises(AppError,match='browser_origin'):
            await browser.open('opaque-owner','https://not-approved.example/',allowed=['https://merchant.example'])
        assert len(requests)==2
    finally:
        await browser.close()


@pytest.mark.parametrize('label,key',[('Web ID','web_id'),('Cuenta','account_number'),('Folio','folio'),
    ('Carril','lane'),('Hora de compra','purchase_time'),('Caseta','toll_station'),('Referencia','reference_number')])
def test_field_meanings_do_not_depend_on_a_merchant_recipe(label,key):
    assert field_key(label)==key


@pytest.mark.parametrize('value',['http://merchant.example','https://user:pass@merchant.example','https://127.0.0.1',
    'https://*.example','https://host.local','https://merchant.example/path'])
def test_operator_domain_configuration_is_explicit_and_private_destinations_rejected(value):
    with pytest.raises(ValueError): domains(value)


def test_operator_domain_configuration_normalizes_without_secret_changes():
    assert domains('https://Merchant.example/, https://billing.example')=='https://merchant.example,https://billing.example'


def test_pending_auxiliary_resource_does_not_block_sdk_navigation_or_trigger_unneeded_permission(billing_client):
    class OptionalResource(FakeBrowser):
        async def open(self,owner,url,*,allowed=None,public_route=False):
            result=await super().open(owner,url,allowed=allowed,public_route=public_route)
            result['blocked_origins']=['https://cdn.example']
            return result
    browser=OptionalResource()
    model=ScriptedModel([[call('portal_browser_observe')],
        [call('portal_browser_navigate',observation_id='aaaaaaaa',reference='bbbbbbbb')],
        [answer('Cuenta disponible.')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser)
    confirm_profile(telegram,chat)
    extractor.ticket['billing_url']='https://merchant.example/'
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    start=len(telegram.messages)
    chat(data=button(telegram,'wa'))
    assert [item[0] for item in browser.calls]==['open','act']
    assert len(telegram.messages)==start+1 and 'Cuenta: Pendiente' in telegram.messages[-1][1]
    assert all('cdn.example' not in message[1] for message in telegram.messages[start:])
    assert not any(value.startswith('wa:') for _,value in telegram.messages[-1][2])
    assert mail.sent==sat.calls==[] and len(extractor.calls)==2


def test_empty_page_does_not_ask_again_for_resources_covered_by_route_permission(billing_client):
    class EmptyPage(FakeBrowser):
        async def open(self,owner,url,*,allowed=None,public_route=False):
            result=snapshot()
            result['blocked_origins']=['https://cdn.example']
            return result
    model=ScriptedModel([[call('portal_browser_observe')],[answer('Revisa el recurso pendiente.')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=EmptyPage())
    confirm_profile(telegram,chat)
    extractor.ticket['billing_url']='https://merchant.example/'
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    chat(data=button(telegram,'wa'))
    assert 'Aún no se encontró el formulario.' in telegram.messages[-1][1]
    assert '¿Permites cargar estos recursos del sitio?' not in telegram.messages[-1][1]
    assert not any(data.startswith('wa:') for _,data in telegram.messages[-1][2])
    assert mail.sent==sat.calls==[]


def test_saved_review_resumes_after_worker_restart_under_existing_human_permission(billing_client):
    class Restarted(FakeBrowser):
        async def observe(self,owner,session):
            self.calls.append(('observe',owner,session))
            raise AppError(404,'browser_session','La revisión venció.')
    browser=Restarted()
    client,app,extractor,*_=billing_client(browser=browser)
    extractor.ticket['billing_url']='https://merchant.example/'
    item=ticket(client)
    grant(client,item['id'])
    path='/api/billing/tickets/'+item['id']+'/browser'
    assert client.post(path).status_code==200
    resumed=client.post(path)
    assert resumed.status_code==200 and [item[0] for item in browser.calls]==['open','observe','open']
    assert app.state.database.usage('owner')['runs']==0
    assert len(app.state.billing.store.list('profile','owner'))==1
