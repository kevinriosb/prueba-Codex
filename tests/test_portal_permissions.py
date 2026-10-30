"""Consentimientos reales del canal con SDK simulado; ningún comercio o API de pago."""
import time

import pytest

from backend.app.errors import AppError
from .billing_fakes import image
from .fake_model import ScriptedModel,answer,call
from .test_billing import billing_client,ticket
from .test_adaptive_browser import FakeBrowser,grant,snapshot
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile


def received(billing_client,model=None,browser=None):
    values=create(billing_client,model,browser=browser or FakeBrowser())
    client,app,extractor,mail,sat,telegram,chat=values
    confirm_profile(telegram,chat)
    extractor.ticket['billing_url']='https://merchant.example/'
    telegram.files['receipt']=image('red')
    chat(document='receipt');chat(data=button(telegram,'an'))
    return values


def test_upload_status_and_permission_request_do_not_open_portal_or_run_ai(billing_client):
    browser=FakeBrowser()
    values=received(billing_client,browser=browser)
    client,app,extractor,mail,sat,telegram,chat=values
    identifier=app.state.telegram._state(owner(app))['last_ticket_id']
    allow=button(telegram,'wa')
    assert '¿Permites revisar este portal' in telegram.messages[-1][1]
    for text in ('/estado','/tickets'):
        chat(text)
    chat(data='bn:'+identifier)
    assert button(telegram,'wa')==allow
    assert browser.calls==[] and app.state.database.usage(owner(app))['runs']==0
    assert len(extractor.calls)==2 and mail.sent==sat.calls==[]
    # Incluso la inspección estática queda detrás de la autorización.
    async def forbidden(_url):
        pytest.fail('No debe abrir el sitio antes del permiso')
    app.state.billing.portal_inspector.inspect=forbidden
    with pytest.raises(AppError,match='portal_permission'):
        client.portal.call(app.state.billing.inspect_billing_portal,owner(app),identifier)
    chat(data='bp:'+identifier)
    assert '¿Permites revisar este portal?' in telegram.messages[-1][1]
    assert browser.calls==[]


def test_cancel_native_button_works_at_update_limit_preserves_data_and_cannot_be_replayed(billing_client):
    browser=FakeBrowser()
    client,app,extractor,mail,sat,telegram,chat=received(billing_client,browser=browser)
    cancel=button(telegram,'wx')
    app.state.settings.billing_max_daily_telegram_updates=1
    chat(data=cancel)
    assert 'cancelada' in telegram.messages[-1][1]
    assert browser.calls==[]
    billing=app.state.billing
    assert len(billing.store.list('profile',owner(app)))==len(billing.store.list('ticket',owner(app)))==1
    assert billing.portal_permissions.allowed(owner(app),billing.store.list('ticket',owner(app))[0])==[]
    chat(data=cancel)
    assert 'ya fue utilizada' in telegram.messages[-1][1]
    assert browser.calls==[]


def test_explicit_permission_is_owned_single_use_and_does_not_expose_fiscal_values(billing_client):
    model=ScriptedModel([[call('portal_browser_observe')],[answer('Campos revisados.')]])
    browser=FakeBrowser()
    client,app,extractor,mail,sat,telegram,chat=received(billing_client,model,browser)
    allow=button(telegram,'wa')
    chat(data=allow,user=456)
    assert 'no encontrado' in telegram.messages[-1][1] and browser.calls==[]
    chat(data=allow)
    assert [item[0] for item in browser.calls]==['open','act']
    count=len(model.inputs)
    chat(data=allow)
    assert len(model.inputs)==count and len(browser.calls)==2
    assert 'ya fue utilizada' in telegram.messages[-1][1]
    assert mail.sent==sat.calls==[]


def test_observed_provider_is_covered_by_native_public_route_permission(billing_client):
    class CrossDomain(FakeBrowser):
        async def open(self,owner,url,*,allowed=None,public_route=False):
            result=await super().open(owner,url,allowed=allowed,public_route=public_route)
            assert allowed==['https://merchant.example']
            result['actions'][0]['permission_origin']='https://billing.example'
            return result
    model=ScriptedModel([[call('portal_browser_observe')],
        [call('portal_browser_navigate',observation_id='aaaaaaaa',reference='bbbbbbbb')],
        [answer('Autoriza el nuevo portal.')],
        [call('portal_browser_observe')],
        [call('portal_browser_navigate',observation_id='dddddddd',reference='eeeeeeee')],
        [answer('Cuenta disponible.')]])
    browser=CrossDomain()
    client,app,extractor,mail,sat,telegram,chat=received(billing_client,model,browser)
    chat(data=button(telegram,'wa'))
    assert [item[0] for item in browser.calls]==['open','act']
    assert not any(data.startswith('wa:') for _,data in telegram.messages[-1][2])
    assert 'Cuenta: No leído' in telegram.messages[-1][1]
    assert mail.sent==sat.calls==[]


def test_permission_api_rejects_unobserved_domains_forged_grants_and_csrf(billing_client):
    browser=FakeBrowser()
    client,app,extractor,*_=billing_client(browser=browser)
    extractor.ticket['billing_url']='https://merchant.example/'
    item=ticket(client)
    path='/api/billing/tickets/'+item['id']+'/portal-permissions'
    assert client.post(path,json={},headers={'X-CSRF-Token':'wrong'}).status_code==403
    assert client.post(path,json={'origins':['https://unobserved.example']}).status_code==403
    assert client.post(path,json={'approve':True}).status_code==422
    review=client.post(path,json={}).json()
    decision='/api/billing/portal-permissions/'+review['id']+'/decision'
    assert client.post(decision,json={'approve':'yes'}).status_code==422
    assert client.post(decision,json={'approve':True},headers={'X-CSRF-Token':'wrong'}).status_code==403
    billing=app.state.billing
    with pytest.raises(AppError) as other:
        billing.portal_permissions.decide('other-user',review['id'],approve=True)
    assert other.value.status==404 and browser.calls==[]
    assert client.post(decision,json={'approve':True}).status_code==200
    assert browser.calls==[]  # API de consentimiento separada de ejecución.
    assert client.post(decision,json={'approve':True}).status_code==409


def test_permissions_expire_and_bind_to_ticket_user_and_exact_source_url(billing_client):
    client,app,extractor,*_=billing_client(browser=FakeBrowser())
    extractor.ticket['billing_url']='https://merchant.example/'
    item=ticket(client)
    billing=app.state.billing
    grant(client,item['id'])
    record=billing.resolve_ticket('owner',item['id'])
    billing.portal_permissions.require('owner',record)
    with pytest.raises(AppError,match='portal_permission'):
        billing.portal_permissions.require('other-user',record)
    extractor.ticket['folio']='OTHER-42'
    other=ticket(client,item['profile_id'],color='blue')
    with pytest.raises(AppError,match='portal_permission'):
        billing.portal_permissions.require('owner',other)
    record['billing_url']='https://merchant.example/another-page'
    billing.store.update('ticket','owner',record['id'],record)
    with pytest.raises(AppError,match='portal_permission'):
        billing.portal_permissions.require('owner',record)
    record['billing_url']='https://merchant.example/'
    billing.store.update('ticket','owner',record['id'],record)
    with billing.database.connection(True) as conn:
        conn.execute("UPDATE billing_records SET expires=? WHERE kind IN ('portal_grant','portal_permission')",(time.time()-1,))
    with pytest.raises(AppError,match='portal_permission'):
        billing.portal_permissions.require('owner',record)
    grant(client,item['id'])  # Metadatos vencidos no chocan con el índice único.
    assert len(billing.store.list('profile','owner'))==1


def test_pending_permission_becomes_invalid_when_ticket_url_changes(billing_client):
    client,app,extractor,*_=billing_client(browser=FakeBrowser())
    extractor.ticket['billing_url']='https://merchant.example/'
    item=ticket(client)
    billing=app.state.billing
    request=billing.portal_permissions.request('owner',item['id'])
    data=billing.resolve_ticket('owner',item['id'])
    data['billing_url']='https://different.example/'
    billing.store.update('ticket','owner',item['id'],data)
    with pytest.raises(AppError,match='portal_permission_changed'):
        billing.portal_permissions.decide('owner',request['id'],approve=True)
    assert billing.store.list('portal_grant','owner')==[]


def test_typed_permission_cannot_replace_native_confirmation_even_when_model_asks_to_navigate(billing_client):
    browser=FakeBrowser()
    model=ScriptedModel([[call('portal_browser_observe')],[answer('Pulsa Permitir.')]])
    client,app,extractor,mail,sat,telegram,chat=received(billing_client,model,browser)
    chat('Sí, permito usar el portal')
    assert browser.calls==[] and app.state.billing.store.list('portal_grant',owner(app))==[]
    assert '¿Permites revisar este portal?' in telegram.messages[-1][1]
    assert button(telegram,'wa')
    assert not any('permission' in tool.name for tool in app.state.service.tools)


def test_cancelling_running_review_closes_session_revokes_grants_and_preserves_profile(billing_client):
    browser=FakeBrowser()
    client,app,extractor,*_=billing_client(browser=browser)
    extractor.ticket['billing_url']='https://merchant.example/'
    item=ticket(client)
    grant(client,item['id'])
    assert client.post('/api/billing/tickets/'+item['id']+'/browser').status_code==200
    root=client.post('/api/billing/tickets/'+item['id']+'/portal-permissions',json={}).json()
    response=client.post('/api/billing/portal-permissions/'+root['id']+'/decision',json={'approve':False})
    assert response.status_code==200
    assert [entry[0] for entry in browser.calls]==['open','close']
    assert client.post('/api/billing/tickets/'+item['id']+'/browser').status_code==403
    billing=app.state.billing
    assert billing.store.list('browser_review','owner')==[]
    assert billing.resolve_ticket('owner',item['id'])['profile_id']==item['profile_id']
    assert len(billing.store.list('profile','owner'))==1


def test_old_browser_domains_environment_cannot_bypass_human_permission(billing_client):
    browser=FakeBrowser()
    client,app,extractor,*_=billing_client(browser=browser,billing_browser_origins='https://merchant.example')
    extractor.ticket['billing_url']='https://merchant.example/'
    item=ticket(client)
    assert client.post('/api/billing/tickets/'+item['id']+'/browser').status_code==403
    assert browser.calls==[]
