"""Recuperación de la revisión pública sin reiniciar perfiles ni el chat general."""
import json
import time
import uuid

import pytest

from backend.app.errors import AppError
from .billing_fakes import image
from .fake_model import ScriptedModel,answer,call
from .test_adaptive_browser import FakeBrowser,snapshot
from .test_billing import billing_client
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile


def receive(factory,model,**settings):
    class UnclearRoute(FakeBrowser):
        # La recuperación usa un control sin atajo inequívoco: lo elige el SDK.
        async def open(self,*args,**kwargs):
            result=await super().open(*args,**kwargs)
            result['actions'][0]['label']='Explorar opciones'
            return result
        async def observe(self,*args,**kwargs):
            result=await super().observe(*args,**kwargs)
            result['actions'][0]['label']='Explorar opciones'
            return result
    browser=UnclearRoute()
    values=create(factory,model,browser=browser,**settings)
    client,app,extractor,mail,sat,telegram,chat=values
    confirm_profile(telegram,chat)
    extractor.ticket['billing_url']='https://merchant.example/'
    telegram.files['receipt']=image('red')
    chat(document='receipt');chat(data=button(telegram,'an'))
    return values,browser


@pytest.mark.parametrize('fault',['turns','blocked','history','pending'])
def test_portal_review_is_not_blocked_by_the_general_conversation(billing_client,fault):
    model=ScriptedModel([[answer('Mensaje general guardado.')],
        [call('portal_browser_observe')],
        [call('portal_browser_navigate',observation_id='aaaaaaaa',reference='bbbbbbbb')],
        [answer('Cuenta disponible.')]])
    values,browser=receive(billing_client,model,max_conversations=1)
    client,app,extractor,mail,sat,telegram,chat=values
    chat('Mensaje general que se debe conservar.')
    state=app.state.billing.store.find('channel_state',owner(app),'telegram')
    identifier=state['conversation_id']
    pending=json.dumps({'id':str(uuid.uuid4()),'expires':time.time()+600,'tools':[{'name':'private_write'}],'state':{}})
    changes={'turns':('turns',app.state.settings.max_conversation_turns),'blocked':('blocked',1),
        'history':('history',json.dumps([{'role':'user','content':'historial-anterior-'*5000}])),
        'pending':('pending',pending)}
    column,value=changes[fault]
    with app.state.database.connection(True) as connection:
        connection.execute('UPDATE conversations SET '+column+'=? WHERE id=?',(value,identifier))
    before=app.state.database.get_conversation(identifier,owner(app))
    chat(data=button(telegram,'wa'))
    assert [item[0] for item in browser.calls]==['open','act']
    assert 'Cuenta: Pendiente' in telegram.messages[-1][1]
    after=app.state.database.get_conversation(identifier,owner(app))
    assert all(after[key]==before[key] for key in ('history','pending','turns','blocked','messages'))
    assert app.state.billing.store.find('channel_state',owner(app),'telegram')['conversation_id']==identifier
    assert [item['id'] for item in app.state.database.list_conversations(owner(app))]==[identifier]
    assert len(model.inputs[-3])==1 and 'historial-anterior' not in str(model.inputs[-3])
    assert app.state.database.usage(owner(app))['runs']==2
    assert len(extractor.calls)==2 and mail.sent==sat.calls==[]


def test_old_model_reference_refreshes_observation_without_executing_the_old_click(billing_client):
    model=ScriptedModel([[call('portal_browser_observe')],[answer('Enlace observado.')],
        [call('portal_browser_navigate',observation_id='aaaaaaaa',reference='bbbbbbbb')],
        [call('portal_browser_navigate',observation_id='dddddddd',reference='eeeeeeee')],
        [answer('Cuenta disponible.')]])
    values,browser=receive(billing_client,model)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'wa'))
    # La referencia anterior no se admite en una ejecución nueva.
    chat(data=button(telegram,'bn'))
    assert [item[0] for item in browser.calls]==['open','observe','act']
    assert browser.calls[-1][-2:]==('dddddddd','eeeeeeee')
    assert 'Cuenta: Pendiente' in telegram.messages[-1][1]
    assert app.state.billing.store.find('channel_state',owner(app),'telegram').get('conversation_id') is None
    assert app.state.database.usage(owner(app))['runs']==2
    assert mail.sent==sat.calls==[]


def test_stale_native_button_refreshes_the_page_without_replaying_a_click_or_using_ai(billing_client):
    model=ScriptedModel([[call('portal_browser_observe')],[answer('Enlace observado.')]])
    values,browser=receive(billing_client,model)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'wa'))
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    client.portal.call(app.state.billing.browse_portal,owner(app),ticket['id'])
    inputs=len(model.inputs)
    chat(data='ba:'+ticket['id']+':aaaaaaaa:bbbbbbbb')
    assert [item[0] for item in browser.calls]==['open','observe','observe']
    assert len(model.inputs)==inputs and 'Revisión pública' in telegram.messages[-1][1]
    assert mail.sent==sat.calls==[]


def test_browser_jobs_keep_the_daily_budget_even_when_the_general_chat_is_full(billing_client):
    model=ScriptedModel([[answer('Mensaje general.')]])
    values,browser=receive(billing_client,model,max_daily_runs=1,max_conversation_turns=1)
    client,app,extractor,mail,sat,telegram,chat=values
    chat('Consume la ejecución de prueba.')
    chat(data=button(telegram,'wa'))
    assert browser.calls==[] and len(model.inputs)==1
    assert app.state.database.usage(owner(app))['runs']==1
    assert 'límite diario' in telegram.messages[-1][1]


def test_public_review_cannot_call_private_or_fiscal_tools(billing_client):
    class ToolCapture(ScriptedModel):
        async def get_response(self,system_instructions,input,model_settings,tools,*args,**kwargs):
            self.tools=[tool.name for tool in tools]
            return await super().get_response(system_instructions,input,model_settings,tools,*args,**kwargs)
    model=ToolCapture([[call('review_fiscal_setup')]])
    values,browser=receive(billing_client,model)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'wa'))
    assert set(model.tools)=={'portal_browser_observe','portal_browser_navigate'}
    assert browser.calls==[] and len(extractor.calls)==2
    assert len(app.state.billing.store.list('profile',owner(app)))==1
    assert app.state.billing.store.list('profile_draft',owner(app))==[]
    assert mail.sent==sat.calls==[]


def test_public_navigation_stops_at_form_even_if_model_asks_to_click_more(billing_client):
    model=ScriptedModel([[call('portal_browser_observe')],
        [call('portal_browser_navigate',observation_id='aaaaaaaa',reference='bbbbbbbb')],
        [call('portal_browser_navigate',observation_id='cccccccc',reference='bbbbbbbb')],
        [answer('Cuenta disponible.')]])
    values,browser=receive(billing_client,model,max_agent_turns=5)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'wa'))
    assert [item[0] for item in browser.calls]==['open','act']
    assert 'form_found' in str(model.inputs[-1])
    assert 'Cuenta: Pendiente' in telegram.messages[-1][1]
    assert app.state.database.usage(owner(app))['runs']==1


@pytest.mark.parametrize('permission_expired',[False,True])
def test_expired_native_review_refreshes_without_ai_or_replaying_old_action(billing_client,permission_expired):
    model=ScriptedModel([[call('portal_browser_observe')],[answer('Enlace observado.')]])
    values,browser=receive(billing_client,model)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'wa'))
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    with app.state.database.connection(True) as connection:
        connection.execute("UPDATE billing_records SET expires=0 WHERE kind='browser_review'")
        if permission_expired:
            connection.execute("UPDATE billing_records SET expires=0 WHERE kind='portal_grant'")
    inputs=len(model.inputs)
    chat(data='ba:'+ticket['id']+':aaaaaaaa:bbbbbbbb')
    assert [item[0] for item in browser.calls]==(['open'] if permission_expired else ['open','open'])
    assert len(model.inputs)==inputs
    expected='¿Permites revisar' if permission_expired else 'Revisión pública'
    assert expected in telegram.messages[-1][1]


def test_legacy_database_migration_preserves_existing_chat_and_limits(tmp_path):
    from backend.app.database import Database
    path=tmp_path/'legacy.sqlite3'
    old=Database(path)
    old.configure_owner('admin','test-hash-only')
    chat=old.create_conversation('owner',1)
    history=json.dumps([{'role':'user','content':'Historial anterior conservado.'}])
    with old.connection(True) as connection:
        connection.execute('UPDATE conversations SET history=?,turns=20 WHERE id=?',(history,chat['id']))
        connection.execute('DROP INDEX one_portal_review_per_owner')
        connection.execute('ALTER TABLE conversations DROP COLUMN purpose')
    migrated=Database(path)
    before=migrated.get_conversation(chat['id'],'owner')
    assert before['history']==history and before['purpose']=='chat' and before['turns']==20
    job=migrated.prepare_portal_review('owner')
    assert job['purpose']=='portal_review' and job['history']=='[]'
    assert [item['id'] for item in migrated.list_conversations('owner')]==[chat['id']]
    with pytest.raises(AppError) as failure:
        migrated.create_conversation('owner',1)
    assert failure.value.code=='conversation_limit'


@pytest.mark.parametrize('code',['browser_stale','browser_session'])
def test_worker_rejects_old_click_then_only_refreshes_without_replaying(billing_client,code):
    model=ScriptedModel([[call('portal_browser_observe')],
        [call('portal_browser_navigate',observation_id='aaaaaaaa',reference='bbbbbbbb')],
        [answer('Página actualizada.')]])
    values,browser=receive(billing_client,model)
    client,app,extractor,mail,sat,telegram,chat=values
    async def stale_action(owner,session,observed,reference):
        browser.calls.append(('act',owner,session,observed,reference))
        raise AppError(409 if code=='browser_stale' else 404,code,'Página anterior.')
    browser.act=stale_action
    chat(data=button(telegram,'wa'))
    assert [item[0] for item in browser.calls]==['open','act','observe' if code=='browser_stale' else 'open']
    assert 'Revisión pública' in telegram.messages[-1][1]
    assert 'Página anterior.' not in telegram.messages[-1][1]
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    assert ticket['portal_browser_error'] is None
    assert mail.sent==sat.calls==[]


def test_large_public_page_pauses_review_without_claiming_general_chat_is_full(billing_client):
    model=ScriptedModel([[call('portal_browser_observe')],[call('portal_browser_observe')],
        [answer('Controles disponibles.')]])
    values,browser=receive(billing_client,model,max_context_bytes=4000)
    client,app,extractor,mail,sat,telegram,chat=values
    def wide():
        return snapshot(fields=[{'label':'Campo público '+str(index)+'x'*70,'key':None,'required':False,
            'kind':'input','options':[]} for index in range(24)],actions=[{'reference':format(index,'08x'),
            'label':'Acceso público '+str(index)+'x'*70,'kind':'link'} for index in range(24)])
    async def open_page(*args,**kwargs): return wide()
    async def observe_page(*args,**kwargs): return wide()
    browser.open=open_page
    browser.observe=observe_page
    start=len(telegram.messages)
    chat(data=button(telegram,'wa'))
    texts=[text for _,text,_ in telegram.messages[start:]]
    assert any('Revisión pausada' in text for text in texts)
    assert any('Revisión pública' in text for text in texts)
    assert all('/nuevo' not in text for text in texts)
    assert mail.sent==sat.calls==[]


@pytest.mark.parametrize('identifier',['not-a-ticket','missing','own-other-ticket','foreign-ticket'])
def test_model_identifier_cannot_change_the_ticket_selected_in_telegram(billing_client,identifier):
    model=ScriptedModel()
    values,browser=receive(billing_client,model)
    client,app,extractor,mail,sat,telegram,chat=values
    selected=app.state.billing.store.list('ticket',owner(app))[0]
    selected['portal_browser_error']={'code':'browser_stale','message':'Error antiguo de otra revisión.'}
    app.state.billing.store.update('ticket',owner(app),selected['id'],selected)
    if identifier=='missing':
        identifier=str(uuid.uuid4())
    elif identifier in {'own-other-ticket','foreign-ticket'}:
        other_owner=owner(app) if identifier=='own-other-ticket' else 'foreign-owner'
        other={key:value for key,value in selected.items() if key!='id' and not key.startswith('_')}
        other['billing_url']='https://unselected.example/'
        identifier=app.state.billing.store.add('ticket',other_owner,other)['id']
    model.outputs=[[call('portal_browser_observe',identifier=identifier)],
        [call('portal_browser_navigate',observation_id='aaaaaaaa',reference='bbbbbbbb')],
        [answer('Cuenta disponible.')]]
    chat(data=button(telegram,'wa'))
    assert [item[0] for item in browser.calls]==['open','act']
    assert browser.calls[0][2]=='https://merchant.example/'
    assert 'Cuenta: Pendiente' in telegram.messages[-1][1]
    assert app.state.billing.store.get('ticket',owner(app),selected['id'])['portal_browser_error'] is None
    assert len(extractor.calls)==2 and mail.sent==sat.calls==[]


@pytest.mark.parametrize('runtime_failure',[False,True])
def test_invalid_model_arguments_show_current_failure_instead_of_an_old_page_error(billing_client,runtime_failure):
    invalid=call('portal_browser_observe').model_copy(update={'arguments':'{invalid-json'})
    model=ScriptedModel([[call('portal_browser_observe') if runtime_failure else invalid],[answer('Revisa el portal.')]])
    values,browser=receive(billing_client,model)
    client,app,extractor,mail,sat,telegram,chat=values
    selected=app.state.billing.store.list('ticket',owner(app))[0]
    selected['portal_browser_error']={'code':'browser_stale','message':'La página cambió. Actualiza la revisión.'}
    app.state.billing.store.update('ticket',owner(app),selected['id'],selected)
    if runtime_failure:
        async def failed_open(*args,**kwargs):
            raise RuntimeError('private-provider-details-not-to-show')
        browser.open=failed_open
    chat(data=button(telegram,'wa'))
    assert browser.calls==[]
    expected='No se completó este paso' if runtime_failure else 'instrucción de navegación no fue válida'
    assert expected in telegram.messages[-1][1]
    assert 'La página cambió' not in telegram.messages[-1][1]
    assert 'private-provider-details-not-to-show' not in str(telegram.messages)
    assert app.state.billing.store.get('ticket',owner(app),selected['id'])['portal_browser_error']['code']==('browser_tool_failure' if runtime_failure else 'browser_tool_input')
    assert len(extractor.calls)==2 and mail.sent==sat.calls==[]
