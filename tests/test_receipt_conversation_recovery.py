"""Una foto de corrección y un reintento continúan la solicitud seleccionada."""
import pytest

from .billing_fakes import image
from .test_billing import billing_client
from .test_official_portal_discovery import Modern,modern
from .test_telegram_agent import button,owner
from .test_portal_preparation import ROOT


@pytest.mark.parametrize('caption',[None,'Te subo la foto nuevamente','Es el mismo ticket, ahora más claro'])
def test_a_new_photo_after_missing_data_updates_the_same_request(billing_client,caption):
    extractor=Modern();extractor.found=None;extractor.ticket['total']=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    ticket=app.state.telegram.workflow.active(owner(app));old_document=ticket['document_id']
    telegram.files['clearer']=image('blue');chat(document='clearer',caption=caption)
    assert len(app.state.billing.store.list('ticket',owner(app)))==1
    if caption is None:
        assert app.state.telegram.workflow.active(owner(app))['document_id']==old_document
        chat(data=next(data for label,data in telegram.messages[-1][2] if label=='Es el mismo ticket'))
    current=app.state.telegram.workflow.active(owner(app))
    assert current['id']==ticket['id'] and current['document_id']!=old_document
    assert 'misma solicitud' in telegram.messages[-1][1]
    approval=button(telegram,'xa')
    before=len(extractor.calls)
    chat(data=approval,user=456)
    assert len(extractor.calls)==before
    chat(data=approval)
    current=app.state.billing.resolve_ticket(owner(app),ticket['id'])
    assert current['total']=='116.00' and not current.get('portal_source_pending')
    assert len(app.state.billing.store.list('ticket',owner(app)))==1
    assert len(app.state.billing.store.list('profile',owner(app)))==1
    assert mail.sent==sat.calls==[]


@pytest.mark.parametrize('as_photo',[False,True])
def test_a_promised_replacement_or_telegram_photo_keeps_a_complete_request(billing_client,as_photo):
    extractor=Modern();extractor.found=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    previous=app.state.telegram.workflow.active(owner(app))
    telegram.files['clearer']=image('blue')
    if as_photo:
        chat.sequence+=1
        response=client.post('/integrations/telegram/webhook',json={'update_id':chat.sequence,
            'message':{'from':{'id':123},'chat':{'id':123,'type':'private'},
                'photo':[{'file_id':'clearer','file_size':1000}], 'caption':'Te subo la foto nuevamente'}},
            headers={'X-Telegram-Bot-Api-Secret-Token':'x'*40})
        assert response.status_code==200
    else:
        chat('Te mando otra foto del mismo ticket')
        chat(document='clearer')
    assert app.state.telegram.workflow.active(owner(app))['id']==previous['id']
    assert len(app.state.billing.store.list('ticket',owner(app)))==1
    approval=button(telegram,'xa');chat(data=approval)
    count=len(extractor.calls);chat(data=approval)
    assert len(extractor.calls)==count and mail.sent==sat.calls==[]


def test_a_replaced_photo_approval_cannot_read_a_later_photo(billing_client):
    extractor=Modern();extractor.found=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    telegram.files.update(first_clearer=image('blue'),second_clearer=image('yellow'))
    chat(document='first_clearer',caption='Otra foto del mismo ticket');stale=button(telegram,'xa')
    chat(document='second_clearer',caption='Otra foto del mismo ticket')
    current=button(telegram,'xa');count=len(extractor.calls)
    field_reads=sum(call[0]=='fields' for call in extractor.calls)
    chat(data=stale)
    assert len(extractor.calls)==count and len(app.state.billing.store.list('ticket',owner(app)))==1
    chat(data=current)
    assert sum(call[0]=='fields' for call in extractor.calls)==field_reads+1
    assert mail.sent==sat.calls==[]


def test_resending_an_earlier_photo_reuses_its_owned_source_and_previous_reading(billing_client):
    extractor=Modern();extractor.found=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    previous=app.state.telegram.workflow.active(owner(app))
    telegram.files['clearer']=image('blue')
    chat(document='clearer',caption='Otra foto del mismo ticket');chat(data=button(telegram,'xa'))
    verified=app.state.telegram.workflow.active(owner(app))['document_id']
    chat(document='receipt',caption='Te subo la foto nuevamente')
    assert app.state.telegram.workflow.active(owner(app))['document_id']==previous['document_id']
    chat(data=button(telegram,'xa'))
    count=len(extractor.calls)
    chat(document='clearer',caption='Te subo la foto nuevamente')
    current=app.state.telegram.workflow.active(owner(app))
    assert current['id']==previous['id'] and current['document_id']==verified
    assert len(app.state.billing.store.list('ticket',owner(app)))==1
    assert len(extractor.calls)==count and not current['portal_source_pending']
    assert mail.sent==sat.calls==[]


def test_explicit_new_receipt_overrides_a_pending_field_without_using_it_as_a_crop(billing_client):
    extractor=Modern();extractor.found=None;extractor.ticket['total']=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    previous=app.state.telegram.workflow.active(owner(app))
    chat(data='td:'+previous['id']+':total')
    telegram.files['other']=image('blue');chat(document='other',caption='Este es otro ticket nuevo')
    assert len(app.state.billing.store.list('ticket',owner(app)))==2
    current=app.state.telegram.workflow.active(owner(app))
    assert current['id']!=previous['id'] and button(telegram,'an').endswith(current['document_id'])
    assert app.state.billing.store.list('portal_field_photo',owner(app))==[]
    assert mail.sent==sat.calls==[]


def test_promising_the_full_receipt_clears_an_old_single_field_prompt(billing_client):
    extractor=Modern();extractor.found=None;extractor.ticket['total']=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    ticket=app.state.telegram.workflow.active(owner(app))
    chat(data='td:'+ticket['id']+':total')
    chat('Te mando otra foto del mismo ticket')
    telegram.files['full']=image('blue');chat(document='full')
    assert app.state.billing.store.list('portal_field_photo',owner(app))==[]
    assert button(telegram,'xa') and len(app.state.billing.store.list('ticket',owner(app)))==1
    chat(data=button(telegram,'xa'))
    state=app.state.telegram._state(owner(app))
    assert not state.get('receipt_update_ticket') and not state.get('ticket_input')
    assert mail.sent==sat.calls==[]


def test_a_corrective_photo_supersedes_a_previous_menu_for_the_active_request(billing_client):
    extractor=Modern();extractor.found=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    ticket=app.state.telegram.workflow.active(owner(app))
    chat('/cancelar')
    assert app.state.telegram.workflow.pending(owner(app)) is not None
    telegram.files['clearer']=image('blue');chat(document='clearer',caption='Te subo la foto nuevamente')
    count=sum(call[0]=='fields' for call in extractor.calls)
    chat(data=button(telegram,'xa'))
    assert app.state.telegram.workflow.pending(owner(app)) is None
    assert sum(call[0]=='fields' for call in extractor.calls)==count+1
    assert app.state.telegram.workflow.active(owner(app))['id']==ticket['id']
    assert mail.sent==sat.calls==[]


def test_search_on_the_active_request_supersedes_a_previous_menu(billing_client):
    extractor=Modern();extractor.found=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'));search=button(telegram,'df')
    chat('/cancelar')
    assert app.state.telegram.workflow.pending(owner(app)) is not None
    extractor.found=ROOT;chat(data=search)
    assert len(extractor.searches)==2 and browser.calls==['open','act']
    assert app.state.telegram.workflow.pending(owner(app)) is None
    assert mail.sent==sat.calls==[]


def test_search_button_retries_a_cached_failure_and_then_opens_the_form(billing_client):
    extractor=Modern();extractor.found=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'an'))
    assert len(extractor.searches)==1 and browser.calls==[]
    extractor.found=ROOT
    chat(data=button(telegram,'df'))
    assert len(extractor.searches)==2 and browser.calls==['open','act']
    assert 'Formulario encontrado' in telegram.messages[-1][1]
    assert len(app.state.billing.store.list('portal_discovery',owner(app)))==2
    assert mail.sent==sat.calls==[]


def test_an_unsuccessful_search_retry_is_reported_and_still_counts_towards_its_limit(billing_client):
    extractor=Modern();extractor.found=None
    values,browser=modern(billing_client,extractor)
    client,app,extractor,mail,sat,telegram,chat=values
    app.state.settings.billing_max_daily_extractions=2
    chat(data=button(telegram,'an'))
    chat(data=button(telegram,'df'))
    assert len(extractor.searches)==2 and browser.calls==[]
    assert 'No encontré' in telegram.messages[-1][1]
    chat(data=button(telegram,'df'))
    assert len(extractor.searches)==2 and 'límite de búsquedas' in telegram.messages[-1][1]
    assert mail.sent==sat.calls==[]
