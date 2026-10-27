import copy
import time
import uuid

import pytest

from backend.app.billing.portal_memory import brand_key
from backend.app.errors import AppError
from .billing_fakes import FISCAL,image
from .fake_model import ScriptedModel,answer,call
from .test_adaptive_browser import FakeBrowser,grant,snapshot
from .test_billing import billing_client,ticket
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile

ROOT='https://group.example/'
FORM='https://provider.example/form?opc=Dominos'


class BrandBrowser(FakeBrowser):
    def __init__(self):
        super().__init__()
        self.allowed=[]
    def brands(self,observed='aaaaaaaa'):
        result=snapshot(actions=[
            {'reference':'bbbbbbbb','label':'Domino’s','kind':'link','brand':True,'permission_origin':'https://provider.example'},
            {'reference':'cccccccc','label':'Otra marca','kind':'link','brand':True,'permission_origin':'https://other.example'}],observed=observed)
        result['page']=ROOT
        return result
    def form(self):
        result=snapshot(fields=[{'label':'RFC','key':'rfc','required':True,'kind':'input','options':[],'source_url':FORM}],observed='dddddddd')
        result.update(page='https://provider.example/form',bookmark_url=FORM)
        return result
    async def open(self,owner,url,*,allowed=None):
        self.calls.append(('open',owner,url))
        self.allowed=allowed or []
        return self.form() if url==FORM else self.brands()
    async def act(self,owner,session,observed,reference):
        assert 'https://provider.example' in self.allowed
        self.calls.append(('act',owner,reference))
        return self.form()
    async def allow(self,owner,session,allowed):
        self.calls.append(('allow',owner,allowed))
        self.allowed=allowed
        return self.brands('eeeeeeee')


def receive(billing_client,*,merchant="Dominos"):
    browser=BrandBrowser()
    model=ScriptedModel([[call('portal_browser_observe')],[answer('Elige la marca.')]])
    values=create(billing_client,model,browser=browser)
    client,app,extractor,mail,sat,telegram,chat=values
    confirm_profile(telegram,chat)
    extractor.ticket.update(merchant=merchant,billing_url=ROOT)
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    chat(data=button(telegram,'wa'))
    return values,browser,model


def arrive_and_save(billing_client,**options):
    values,browser,model=receive(billing_client,**options)
    client,app,extractor,mail,sat,telegram,chat=values
    choice=button(telegram,'ba')
    assert len(choice.encode())<=64
    chat(data=choice)
    assert 'https://provider.example' in telegram.messages[-1][1]
    assert [item[0] for item in browser.calls]==['open']
    chat(data=button(telegram,'wa'))
    assert [item[0] for item in browser.calls]==['open','allow','act']
    assert 'Guardar portal' in str(telegram.messages[-1][2])
    chat(data=button(telegram,'bs'))
    saved=app.state.billing.store.list('portal_memory',owner(app))[0]
    return values,browser,model,saved


def test_native_brand_choice_uses_observed_control_and_requires_destination_permission_without_extra_ai(billing_client):
    values,browser,model,saved=arrive_and_save(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    assert len(model.inputs)==2 and app.state.database.usage(owner(app))['runs']==1
    assert saved['url']==FORM and saved['merchant']=='Dominos'
    assert 'guardado para próximos tickets' in telegram.messages[-1][1]
    assert len(extractor.calls)==2 and mail.sent==sat.calls==[]
    assert len(app.state.billing.store.list('profile',owner(app)))==1


def test_next_ticket_reuses_direct_route_only_after_new_permission_and_keeps_original_url(billing_client):
    values,browser,model,saved=arrive_and_save(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    extractor.ticket.update(merchant="DOMINO'S",folio='NEW-999')
    telegram.files['second']=image('blue');chat(document='second');chat(data=button(telegram,'an'))
    new=app.state.billing.store.list('ticket',owner(app))[0]
    assert new['selected_portal']=={'id':saved['id'],'url':FORM} and new['billing_url']==ROOT
    assert 'Portal guardado: '+FORM in telegram.messages[-1][1]
    before=len(browser.calls)
    assert button(telegram,'wa') and len(browser.calls)==before
    model.outputs.extend([[call('portal_browser_observe')],[answer('Formulario listo.')]])
    chat(data=button(telegram,'wa'))
    assert browser.calls[-1][0]=='open' and browser.calls[-1][2]==FORM
    assert browser.allowed==['https://provider.example']
    assert mail.sent==sat.calls==[]


def test_saved_routes_are_private_and_different_brands_issuers_or_source_sites_do_not_share_them(billing_client):
    values,browser,model,saved=arrive_and_save(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    billing=app.state.billing
    original=billing.store.list('ticket',owner(app))[0]
    assert billing.portal_memory.suggestion(owner(app),original)['id']==saved['id']
    assert billing.portal_memory.suggestion('other-user',original) is None
    for change in ({'merchant':'Otra marca'},{'issuer_rfc':'CCC010101CCC'},{'billing_url':'https://different.example/'}):
        assert billing.portal_memory.suggestion(owner(app),original|change) is None
    with pytest.raises(AppError) as error:
        billing.portal_memory.forget('other-user',saved['id'])
    assert error.value.status==404
    assert client.get('/api/billing/portals').json()==[]  # La identidad web es distinta.


def test_saved_route_expiry_and_forgetting_fall_back_to_ticket_without_erasing_profiles(billing_client):
    values,browser,model,saved=arrive_and_save(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    selected=app.state.billing.portal_memory.select(owner(app),saved['ticket_id'])
    assert app.state.billing.portal_memory.entry_url(owner(app),selected)==FORM
    chat('/portales');assert FORM in telegram.messages[-1][1]
    chat(data=button(telegram,'bf'))
    assert app.state.billing.portal_memory.entry_url(owner(app),selected)==ROOT
    assert len(app.state.billing.store.list('profile',owner(app)))==1
    assert len(app.state.billing.store.list('ticket',owner(app)))==1


def web_form(billing_client,**overrides):
    browser=BrandBrowser()
    client,app,extractor,*_=billing_client(browser=browser)
    extractor.ticket.update(merchant='Dominos',billing_url=ROOT)
    item=ticket(client)
    grant(client,item['id'])
    path='/api/billing/tickets/'+item['id']+'/browser'
    root=client.post(path).json()
    grant(client,item['id'],origins=['https://provider.example'])
    current=app.state.billing.browser_review('owner',item['id'])
    # La confirmación actualizó las referencias; usar la revisión nueva.
    response=client.post(path+'/actions',json={'observation_id':current['snapshot']['observation_id'],'reference':'bbbbbbbb'})
    assert response.status_code==200,response.json()
    if overrides:
        record=app.state.billing.browser_review('owner',item['id'])
        record['snapshot'].update(overrides)
        app.state.billing.store.update('browser_review','owner',record['id'],record)
    return client,app,item


def test_bookmark_api_needs_csrf_confirmation_and_current_observation_and_ignores_no_model_url(billing_client):
    client,app,item=web_form(billing_client)
    path='/api/billing/tickets/'+item['id']+'/portal-bookmark'
    data={'confirmed':True,'observation_id':'dddddddd'}
    assert client.post(path,json=data,headers={'X-CSRF-Token':'wrong'}).status_code==403
    assert client.post(path,json=data|{'confirmed':False}).status_code==409
    assert client.post(path,json=data|{'observation_id':'aaaaaaaa'}).status_code==409
    assert client.post(path,json=data|{'url':'https://invented.example'}).status_code==422
    assert client.post(path,json=data).status_code==200
    saved=client.get('/api/billing/portals').json()[0]
    assert saved['url']==FORM
    assert client.post(path,json=data).json()['id']==saved['id']
    assert client.request('DELETE','/api/billing/portals/'+saved['id'],json={'confirmed':False}).status_code==409
    assert client.request('DELETE','/api/billing/portals/'+saved['id'],json={'confirmed':True}).status_code==200
    assert client.get('/api/billing/portals').json()==[]


@pytest.mark.parametrize('change',[
    {'captcha':True}, {'network_blocked':True}, {'fields':[]},
    {'fields':[{'key':'email','source_url':FORM}]},
    {'fields':[{'key':'rfc','source_url':None}]},
    {'fields':[{'key':'rfc','source_url':'https://provider.example/form?session=private'}]},
    {'fields':[{'key':'rfc','source_url':'https://provider.example/T-1234'}]},
])
def test_ephemeral_or_non_invoice_pages_cannot_be_saved(billing_client,change):
    client,app,item=web_form(billing_client,**change)
    result=client.post('/api/billing/tickets/'+item['id']+'/portal-bookmark',json={'confirmed':True,'observation_id':'dddddddd'})
    assert result.status_code==409 and client.get('/api/billing/portals').json()==[]


@pytest.mark.parametrize('left,right',[('Dominos','Domino’s'),('Logo Domino-s','DOMINOS'),('P.F. Chang’s','PF Changs')])
def test_brand_matching_ignores_punctuation_and_logo_prefix(left,right):
    assert brand_key(left)==brand_key(right)


def test_route_persists_encrypted_after_application_restart(billing_client):
    client,app,item=web_form(billing_client)
    saved=client.post('/api/billing/tickets/'+item['id']+'/portal-bookmark',json={'confirmed':True,'observation_id':'dddddddd'}).json()
    with app.state.billing.database.connection() as connection:
        payload=connection.execute("SELECT payload FROM billing_records WHERE id=?",(saved['id'],)).fetchone()[0]
        assert FORM.encode() not in payload
    second,new_app,*_=billing_client(path=app.state.settings.app_database_path,browser=BrandBrowser())
    assert second.get('/api/billing/portals').json()[0]['url']==FORM
    existing=new_app.state.billing.resolve_ticket('owner',item['id'])
    assert new_app.state.billing.portal_memory.suggestion('owner',existing)['id']==saved['id']


def test_expired_route_is_not_used_and_does_not_change_fiscal_data(billing_client):
    client,app,item=web_form(billing_client)
    client.post('/api/billing/tickets/'+item['id']+'/portal-bookmark',json={'confirmed':True,'observation_id':'dddddddd'})
    selected=app.state.billing.portal_memory.select('owner',item['id'])
    with app.state.billing.database.connection(True) as connection:
        connection.execute("UPDATE billing_records SET expires=? WHERE kind='portal_memory'",(time.time()-1,))
    assert app.state.billing.portal_memory.entry_url('owner',selected)==ROOT
    assert app.state.billing.portal_memory.suggestion('owner',selected) is None
    assert app.state.billing.store.list('profile','owner')[0]['rfc']==FISCAL['rfc']


def test_unknown_form_origin_is_not_silently_trusted_for_future_tickets(billing_client):
    client,app,item=web_form(billing_client,fields=[{'key':'rfc','source_url':'https://unapproved.example/form'}])
    response=client.post('/api/billing/tickets/'+item['id']+'/portal-bookmark',json={'confirmed':True,'observation_id':'dddddddd'})
    assert response.status_code==403 and client.get('/api/billing/portals').json()==[]


def test_company_with_several_brands_does_not_teach_one_portal_for_the_whole_issuer(billing_client):
    values,browser,model,saved=arrive_and_save(billing_client,merchant='Operadora Ejemplo')
    client,app,extractor,mail,sat,telegram,chat=values
    existing=app.state.billing.store.list('ticket',owner(app))[0]
    assert saved['merchant']=='Domino’s'
    assert app.state.billing.portal_memory.suggestion(owner(app),existing) is None
    assert app.state.billing.portal_memory.suggestion(owner(app),existing|{'merchant':'Dominos'})['id']==saved['id']
    assert app.state.billing.portal_memory.suggestion(owner(app),existing|{'merchant':'Otra marca'}) is None


def test_another_user_cannot_use_brand_or_bookmark_buttons_from_this_ticket(billing_client):
    values,browser,model=receive(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    before=len(browser.calls)
    chat(data=button(telegram,'ba'),user=456)
    assert len(browser.calls)==before and 'no encontrado' in telegram.messages[-1][1]
    chat(data=button(telegram,'ba'))
    chat(data=button(telegram,'wa'))
    chat(data=button(telegram,'bs'),user=456)
    assert app.state.billing.store.list('portal_memory',owner(app))==[]
    assert 'no encontrado' in telegram.messages[-1][1]


@pytest.mark.parametrize('left,right,expected',[
    ('Dominos','Domino’s Pizza',True),
    ('Dom','Dominos',False),
    ('Operadora Ejemplo','Dominos',False),
    ('Marca Uno','Marca Dos',False),
])
def test_extended_logo_label_requires_whole_brand_words(left,right,expected):
    from backend.app.billing.portal_memory import same_brand_label
    assert same_brand_label(left,right) is expected


def test_confirmed_long_logo_label_remembers_the_merchant_name_on_the_ticket(billing_client):
    client,app,item=web_form(billing_client)
    record=app.state.billing.browser_review('owner',item['id'])
    record['brand_label']='Domino’s Pizza'
    app.state.billing.store.update('browser_review','owner',record['id'],record)
    saved=client.post('/api/billing/tickets/'+item['id']+'/portal-bookmark',json={'confirmed':True,'observation_id':'dddddddd'})
    assert saved.status_code==200 and saved.json()['merchant']=='Dominos'
    assert app.state.billing.portal_memory.suggestion('owner',app.state.billing.resolve_ticket('owner',item['id']))
