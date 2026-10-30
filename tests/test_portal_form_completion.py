"""Una revisión pública termina al encontrar el formulario; sin otro turno de IA."""
import json

import pytest

from backend.app.billing.portal_inspection import field_key
from backend.app.billing.models import TicketExtraction
from backend.app.errors import AppError
from .billing_fakes import FISCAL,TICKET,image
from .fake_model import ScriptedModel,answer,call
from .test_adaptive_browser import FakeBrowser,snapshot
from .test_billing import billing_client
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile


FORM='https://provider.example/form?brand=Pizza'


class DirectForm(FakeBrowser):
    protection={}
    async def open(self,owner,url,*,allowed=None,public_route=False):
        self.calls.append(('open',owner,url))
        fields=[{'key':field_key(label),'label':label,'required':True,'kind':'input','options':[],
            'source_url':FORM} for label in ('RFC','Ticket','Tienda','Fecha')]
        result=snapshot(fields=fields,actions=[{'reference':'bbbbbbbb','label':'Facturación','kind':'link'}])
        return result|{'page':FORM.split('?')[0],'bookmark_url':FORM}|self.protection


def prepared(factory,*,browser=None,turns=6):
    # Un segundo turno no es necesario: una ejecución con límite 1 debe bastar.
    model=ScriptedModel([[call('portal_browser_observe')],[answer('No debe necesitar este turno.')]])
    values=create(factory,model,browser=browser or DirectForm(),max_agent_turns=turns)
    client,app,extractor,mail,sat,telegram,chat=values
    confirm_profile(telegram,chat)
    extractor.ticket.update(merchant='Pizza Alfa',billing_url='https://merchant.example/')
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    return values,model


def test_direct_form_finishes_with_one_model_turn_and_no_pause_or_continue_button(billing_client):
    values,model=prepared(billing_client,turns=1)
    client,app,extractor,mail,sat,telegram,chat=values
    start=len(telegram.messages)
    chat(data=button(telegram,'wa'))
    messages=telegram.messages[start:]
    assert len(messages)==1 and 'Formulario encontrado' in messages[0][1]
    assert 'pausada' not in messages[0][1].casefold()
    assert not any('Continuar revisión' in label for label,_ in messages[0][2])
    assert 'Completar Tienda' in str(messages[0][2]) and 'Falta' in messages[0][1]
    assert len(model.inputs)==1 and app.state.database.usage(owner(app))['runs']==1
    assert all(value not in json.dumps(model.inputs) for value in (FISCAL['rfc'],FISCAL['name']))
    assert len(extractor.calls)==2 and mail.sent==sat.calls==[]


def test_pressing_old_continue_button_after_form_does_not_start_another_ai_run(billing_client):
    values,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'wa'))
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    inputs=len(model.inputs);runs=app.state.database.usage(owner(app))['runs']
    chat(data='bn:'+ticket['id'])
    assert len(model.inputs)==inputs and app.state.database.usage(owner(app))['runs']==runs
    assert 'Formulario encontrado' in telegram.messages[-1][1]


@pytest.mark.parametrize('code',['run_timeout','agent_turn_limit','tool_limit','context_limit'])
def test_a_limit_after_saving_the_form_does_not_send_a_false_pause(billing_client,code):
    values,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    async def interrupted(user,chat_id,**kwargs):
        ticket=app.state.billing.store.list('ticket',user)[0]
        await app.state.billing.browse_portal(user,ticket['id'])
        raise AppError(504,code,'simulated limit after the saved observation')
    app.state.telegram.agent.reply=interrupted
    start=len(telegram.messages)
    chat(data=button(telegram,'wa'))
    messages=telegram.messages[start:]
    assert len(messages)==1 and 'Formulario encontrado' in messages[0][1]
    assert 'pausada' not in messages[0][1].casefold()
    assert mail.sent==sat.calls==[]


@pytest.mark.parametrize('protection',[{'network_blocked':True},{'captcha':True},{'login_required':True}])
def test_visible_fields_on_a_protected_page_do_not_suppress_the_real_limit(billing_client,protection):
    browser=DirectForm();browser.protection=protection
    values,model=prepared(billing_client,browser=browser,turns=1)
    *_,telegram,chat=values
    start=len(telegram.messages)
    chat(data=button(telegram,'wa'))
    assert any('Revisión pausada' in message[1] for message in telegram.messages[start:])
    assert all('Formulario encontrado' not in message[1] for message in telegram.messages[start:])


@pytest.mark.parametrize('label',['Tienda*','Número de tienda','Store number','No. de sucursal'])
def test_store_identifier_is_a_separate_recognized_purchase_field(label):
    assert field_key(label)=='store_number'
    assert field_key('RFC de la tienda')=='rfc'
    assert field_key('Nombre de tienda') is None
    assert field_key('Store name') is None


def test_store_number_is_optional_and_not_inferred_from_branch_name():
    assert TicketExtraction(**(TICKET|{'branch':'Sucursal 27 Centro'})).store_number is None
    data=TicketExtraction(**(TICKET|{'store_number':'00027','branch':'Centro'}))
    assert data.store_number=='00027' and data.branch=='Centro' and data.folio==TICKET['folio']


def test_missing_store_can_be_completed_in_telegram_even_after_the_original_file_expires(billing_client):
    values,model=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'wa'))
    ticket=app.state.billing.store.list('ticket',owner(app))[0]
    with app.state.database.connection(True) as connection:
        connection.execute("UPDATE billing_records SET expires=0 WHERE kind='document' AND id=?",(ticket['document_id'],))
        connection.execute("UPDATE billing_records SET expires=0 WHERE kind='browser_review'")
    before=len(model.inputs)
    chat('/estado')
    assert 'Completar Tienda' in str(telegram.messages[-1][2])
    chat(data=button(telegram,'td'))
    assert 'TIENDA' in telegram.messages[-1][1] and 'foto' in telegram.messages[-1][1]
    chat('07001')
    updated=app.state.billing.resolve_ticket(owner(app),ticket['id'])
    assert updated['store_number']=='07001' and updated['folio']==ticket['folio']
    assert len(model.inputs)==before and len(extractor.calls)==2 and mail.sent==sat.calls==[]
    assert '07001' in telegram.messages[-1][1]
    with app.state.database.connection(True) as connection:
        connection.execute("UPDATE billing_records SET expires=0 WHERE kind='browser_review'")
    chat('/estado')
    assert 'Tienda: 07001' in telegram.messages[-1][1]
    assert len(model.inputs)==before and len(extractor.calls)==2


@pytest.mark.parametrize('delivered',[False,True])
def test_expired_attachment_does_not_allow_unanalyzed_or_already_delivered_ticket_edits(billing_client,delivered):
    values,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'wa'))
    user=owner(app);billing=app.state.billing
    ticket=billing.store.list('ticket',user)[0]
    if delivered:
        ticket['delivery']={'message_id':'already-sent'}
    else:
        ticket.pop('is_ticket',None)
    billing.store.update('ticket',user,ticket['id'],ticket)
    with app.state.database.connection(True) as connection:
        connection.execute("UPDATE billing_records SET expires=0 WHERE kind='document' AND id=?",(ticket['document_id'],))
    with pytest.raises(AppError) as denied:
        billing.complete_ticket_field(user,ticket['id'],'store_number','07001')
    assert denied.value.code=='ticket_field'
    with pytest.raises(AppError) as foreign:
        billing.complete_ticket_field('another-owner',ticket['id'],'store_number','07001')
    assert foreign.value.status==404

