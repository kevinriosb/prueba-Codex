import pytest

from .billing_fakes import image
from .fake_model import ScriptedModel
from .test_billing import billing_client
from .test_telegram_agent import button, create, owner
from .test_telegram_documents import confirm_profile


def first_ticket(factory, **options):
    result=create(factory,ScriptedModel(),**options)
    client,app,extractor,mail,sat,telegram,chat=result
    confirm_profile(telegram,chat)
    telegram.files['first']=image('red')
    chat(document='first')
    chat(data=button(telegram,'an'))
    return result,app.state.telegram._state(owner(app))['last_ticket_id']


def choice(telegram,action):
    return next(data for _,data in telegram.messages[-1][2] if data.startswith('fw:') and data.endswith(':'+action))


def test_video_and_reply_to_video_explain_format_without_ai(billing_client):
    result,identifier=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    for extra in ({'video':{'file_id':'live-photo','duration':5}},
            {'text':'Es este','reply_to_message':{'video':{'file_id':'live-photo'}}},{'text':'Es este'}):
        chat.sequence+=1
        response=client.post('/integrations/telegram/webhook',json={'update_id':chat.sequence,
            'message':{'from':{'id':123},'chat':{'id':123,'type':'private'}}|extra},
            headers={'X-Telegram-Bot-Api-Secret-Token':'x'*40})
        assert response.status_code==200
        assert 'video' in telegram.messages[-1][1].lower() and 'PDF' in telegram.messages[-1][1]
    assert app.state.service.agent.model.inputs==[]
    assert telegram.downloads==['csf','first'] and len(extractor.calls)==2


def test_new_upload_pauses_previous_only_after_confirming_it_is_another_ticket(billing_client):
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    telegram.files['second']=image('green')
    chat(document='second')
    assert app.state.telegram.workflow.active(owner(app))['id']==old
    chat(data=button(telegram,'fo'))
    assert len(extractor.calls)==2
    assert not any(data.startswith('fw:') for _,data in telegram.messages[-1][2])
    active=app.state.telegram._state(owner(app))['active_ticket_id']
    assert active!=old and app.state.billing.resolve_ticket(owner(app),old)['telegram_workflow']=='paused'
    extractor.ticket['folio']='SECOND-2'
    chat(data=button(telegram,'an'))
    assert len(extractor.calls)==3 and mail.sent==[]
    assert app.state.billing.resolve_ticket(owner(app),active)['document_id']!=app.state.billing.resolve_ticket(owner(app),old)['document_id']


def test_start_from_zero_clears_old_target_and_does_not_use_ai(billing_client):
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    state=app.state.telegram._state(owner(app))
    state.update(browser_ticket_id=old,portal_upload_ticket=old,ticket_input={'id':old,'field':'folio'})
    app.state.telegram._save_state(owner(app),state)
    chat('Empieza de 0 un nuevo ticket')
    state=app.state.telegram._state(owner(app))
    assert state['workflow_mode']=='awaiting_ticket' and state['active_ticket_id'] is None
    assert all(not state.get(key) for key in ('last_ticket_id','browser_ticket_id','portal_upload_ticket','ticket_input','last_document_id'))
    chat('/estado')
    assert 'nuevo ticket' in telegram.messages[-1][1].lower()
    assert app.state.service.agent.model.inputs==[] and len(extractor.calls)==2


def test_returning_to_previous_request_keeps_new_ticket_without_reading_or_reupload(billing_client):
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    original=app.state.billing.resolve_ticket(owner(app),old)['document_id']
    telegram.files['second']=image('green');chat(document='second',caption='Es otro ticket')
    second=app.state.telegram.workflow.active(owner(app))
    pending=second['document_id']
    chat(data='ts:'+old);chat(data=choice(telegram,'pause'))
    assert app.state.telegram._state(owner(app))['active_ticket_id']==old
    assert app.state.billing.resolve_ticket(owner(app),old)['document_id']==original
    chat('/tickets')
    assert ('ts:'+second['id']) in str(telegram.messages[-1][2])
    chat(data='ts:'+second['id']);chat(data=choice(telegram,'pause'))
    assert button(telegram,'an').endswith(pending)
    assert len(extractor.calls)==2 and telegram.downloads==['csf','first','second']


def test_cancel_retires_pending_approvals_and_grants_preserving_profile(billing_client):
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    billing=app.state.billing;who=owner(app)
    profile=billing.store.list('profile',who)[0]
    approval=billing.store.add('email_approval',who,{'ticket_id':old},state='pending')
    grant=billing.store.add('portal_grant',who,{'ticket_id':old},state='active')
    chat('/cancelar')
    assert billing.store.get('email_approval',who,approval['id'])['_state']=='pending'
    chat(data=choice(telegram,'cancel'))
    assert billing.resolve_ticket(who,old)['telegram_workflow']=='cancelled'
    assert billing.store.get('email_approval',who,approval['id'])['_state']=='rejected'
    assert billing.store.get('portal_grant',who,grant['id'])['_state']=='revoked'
    assert billing.store.list('profile',who)==[profile] and mail.sent==[]
    chat(data='se:'+approval['id'])
    assert choice(telegram,'resume') and mail.sent==[]


def test_uncertain_send_cannot_be_cancelled_by_forged_option(billing_client):
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    app.state.billing.store.add('email_approval',owner(app),{'ticket_id':old},state='unknown')
    chat('/cancelar')
    assert all(not data.endswith(':cancel') for _,data in telegram.messages[-1][2])
    keep=choice(telegram,'keep')
    chat(data=keep.rsplit(':',1)[0]+':cancel')
    assert app.state.telegram.workflow.active(owner(app))['id']==old
    chat('Empezar con otra solicitud')
    assert app.state.telegram.workflow.active(owner(app)) is None and mail.sent==[]


def test_stale_and_foreign_switch_buttons_cannot_choose_a_different_file(billing_client):
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    telegram.files.update(second=image('green'),third=image('yellow'))
    chat(document='second',caption='Es otro ticket');second=app.state.telegram.workflow.active(owner(app))['id']
    chat(data='ts:'+old);stale=choice(telegram,'pause')
    chat(document='third',caption='Es otro ticket');third=app.state.telegram.workflow.active(owner(app))['id']
    chat(data=stale)
    assert app.state.telegram.workflow.active(owner(app))['id']==third
    chat(data='ts:'+second);current=choice(telegram,'pause')
    chat(data=current,user=456)
    assert app.state.billing.store.list('ticket',owner(app,456))==[]
    chat(data=current)
    assert app.state.telegram.workflow.active(owner(app))['id']==second
    count=app.state.billing.store.count('ticket',owner(app))
    chat(data=current)
    assert app.state.billing.store.count('ticket',owner(app))==count and len(extractor.calls)==2


def test_old_analysis_button_asks_before_returning_to_previous_ticket(billing_client):
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    old_button=button(telegram,'an')
    telegram.files['second']=image('green');chat(document='second',caption='Es otro ticket')
    current=app.state.telegram.workflow.active(owner(app))['id']
    chat(data=old_button)
    assert app.state.telegram.workflow.active(owner(app))['id']==current
    assert choice(telegram,'pause') and len(extractor.calls)==2


def test_old_ticket_button_asks_before_switching_the_active_request(billing_client):
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    telegram.files['second']=image('green');chat(document='second',caption='Es otro ticket')
    second=app.state.telegram.workflow.active(owner(app))['id']
    chat(data='ts:'+old)
    assert app.state.telegram.workflow.active(owner(app))['id']==second and choice(telegram,'pause')
    chat(data=choice(telegram,'keep'))
    assert second[:8] in telegram.messages[-1][1] and app.state.telegram.workflow.active(owner(app))['id']==second


def test_pending_change_survives_restart_without_losing_new_document(billing_client):
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    telegram.files['second']=image('green');chat(document='second',caption='Es otro ticket')
    second=app.state.telegram.workflow.active(owner(app))['id']
    pending=app.state.billing.resolve_ticket(owner(app),second)['document_id']
    chat(data='ts:'+old)
    current=choice(telegram,'pause')
    client2,app2,ext2,mail2,sat2,tg2,chat2=create(billing_client,path=app.state.settings.app_database_path)
    chat2.sequence=10000
    chat2('/start')
    assert old[:8] in tg2.messages[-1][1] and choice(tg2,'pause')==current
    chat2(data=current);chat2(data='ts:'+second);chat2(data=choice(tg2,'pause'))
    assert button(tg2,'an').endswith(pending) and ext2.calls==[]


def test_reply_to_photo_receives_it_and_waits_for_identity_before_a_new_request(billing_client):
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    telegram.files['quoted']=image('green')
    chat.sequence+=1
    response=client.post('/integrations/telegram/webhook',json={'update_id':chat.sequence,'message':{
        'from':{'id':123},'chat':{'id':123,'type':'private'},'text':'Es este',
        'reply_to_message':{'photo':[{'file_id':'quoted'}]}}},
        headers={'X-Telegram-Bot-Api-Secret-Token':'x'*40})
    assert response.status_code==200 and app.state.telegram.workflow.active(owner(app))['id']==old
    chat(data=button(telegram,'fo'))
    assert app.state.telegram.workflow.active(owner(app))['id']!=old
    assert telegram.downloads[-1]=='quoted' and app.state.service.agent.model.inputs==[]


def test_new_request_clears_a_stale_csf_upload_prompt(billing_client):
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    state=app.state.telegram._state(owner(app));state['expected_upload']='csf'
    app.state.telegram._save_state(owner(app),state)
    chat('/nuevo')
    telegram.files['second']=image('green');chat(document='second',caption='Es otro ticket')
    document=app.state.billing.document(owner(app),button(telegram,'an').split(':')[1])
    assert document['purpose']=='ticket' and len(extractor.calls)==2


def test_new_chat_is_separate_from_starting_a_new_request(billing_client):
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    chat('/nuevo_chat')
    assert app.state.telegram.workflow.active(owner(app))['id']==old
    chat('/nuevo')
    assert app.state.telegram.workflow.active(owner(app)) is None and app.state.service.agent.model.inputs==[]


def test_bot_menu_does_not_reuse_an_older_ticket_requested_by_the_model(billing_client):
    from .fake_model import call,answer
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    telegram.files['second']=image('green');chat(document='second',caption='Es otro ticket')
    second=app.state.telegram.workflow.active(owner(app))['id']
    app.state.service.agent.model.outputs=[[call('track_invoice_email',identifier=old)],[answer('Revisa la solicitud activa.')]]
    chat('Revisa la compra anterior')
    assert mail.fetch_calls==0 and app.state.telegram.workflow.active(owner(app))['id']==second
    assert second[:8] in telegram.messages[-1][1]


def test_menu_shows_stage_and_identity_at_start_and_after_email_send(billing_client):
    result,identifier=first_ticket(billing_client,billing_live_email_enabled=True)
    client,app,extractor,mail,sat,telegram,chat=result
    chat('/start')
    assert identifier[:8] in telegram.messages[-1][1] and 'Paso 3/4' in telegram.messages[-1][1]
    chat(data='ve:'+identifier);chat(data='pr:'+identifier)
    assert mail.sent==[]
    chat(data=button(telegram,'se'))
    assert len(mail.sent)==1 and identifier[:8] in telegram.messages[-1][1]
    chat('/menu')
    assert 'Esperando la factura' in telegram.messages[-1][1]
    assert app.state.service.agent.model.inputs==[]


def test_full_native_email_flow_ends_with_invoice_and_a_new_request_menu(billing_client):
    from .billing_fakes import xml,pdf
    from .test_billing import response_for
    from backend.app.billing.providers import Attachment
    result,identifier=first_ticket(billing_client,billing_live_email_enabled=True)
    client,app,extractor,mail,sat,telegram,chat=result
    chat(data='ve:'+identifier);chat(data='pr:'+identifier);chat(data=button(telegram,'se'))
    ticket=app.state.billing.resolve_ticket(owner(app),identifier)
    mail.replies=[response_for(ticket,[Attachment('factura.xml','application/xml',xml()),Attachment('factura.pdf','application/pdf',pdf())])]
    chat(data='tr:'+identifier)
    assert sat.calls==[] and 'Completada' not in telegram.messages[-1][1]
    chat(data='sa:'+identifier)
    assert sat.calls==[]
    chat(data=button(telegram,'sv'))
    assert app.state.billing.resolve_ticket(owner(app),identifier)['status']=='FACTURACION_COMPLETADA'
    chat('/menu')
    assert app.state.telegram.workflow.active(owner(app)) is None
    assert any(data.endswith(':new') for _,data in telegram.messages[-1][2])
    chat('/nuevo')
    assert 'nuevo ticket' in telegram.messages[-1][1] and len(mail.sent)==1
    assert len(sat.calls)==1 and app.state.service.agent.model.inputs==[]


def test_cancel_after_send_stops_tracking_and_explains_it_cannot_recall_email(billing_client):
    result,identifier=first_ticket(billing_client,billing_live_email_enabled=True,billing_background_tracking_enabled=True)
    client,app,extractor,mail,sat,telegram,chat=result
    chat(data='ve:'+identifier);chat(data='pr:'+identifier);chat(data=button(telegram,'se'))
    app.state.billing.configure_tracking(owner(app),identifier,enabled=True,confirmed=True)
    chat('/cancelar')
    assert 'no se anula' in telegram.messages[-1][1]
    chat(data=choice(telegram,'cancel'))
    item=app.state.billing.resolve_ticket(owner(app),identifier)
    assert item['delivery'] and not item['tracking']['enabled'] and item['telegram_workflow']=='cancelled'
    assert len(mail.sent)==1 and mail.fetch_calls==0


def test_old_replacement_mode_cannot_retarget_the_new_active_ticket(billing_client):
    result,old=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    telegram.files['second']=image('green');chat(document='second',caption='Es otro ticket')
    current=app.state.telegram.workflow.active(owner(app))
    state=app.state.telegram._state(owner(app));state['portal_upload_ticket']=old
    app.state.telegram._save_state(owner(app),state)
    telegram.files['third']=image('yellow');chat(document='third',caption='Es otro ticket')
    assert app.state.telegram.workflow.active(owner(app))['id']!=current['id']
    assert app.state.billing.resolve_ticket(owner(app),current['id'])['document_id']==current['document_id']
    assert app.state.telegram.workflow.pending(owner(app)) is None


def test_menus_remain_available_when_ai_and_action_budgets_are_exhausted(billing_client):
    result,identifier=first_ticket(billing_client,billing_max_daily_telegram_updates=20)
    client,app,extractor,mail,sat,telegram,chat=result
    while app.state.telegram.requests.usage(owner(app))['updates']<20:
        chat('/documentos')
    chat('/menu')
    assert identifier[:8] in telegram.messages[-1][1]
    home=next(data for _,data in telegram.messages[-1][2] if data.endswith(':continue'))
    chat(data=home)
    assert identifier[:8] in telegram.messages[-1][1] and app.state.service.agent.model.inputs==[]


def test_reupload_of_the_active_ticket_preserves_its_navigation_choice(billing_client):
    result,identifier=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    state=app.state.telegram._state(owner(app))
    state.update(browser_ticket_id=identifier,browser_choice={'ticket_id':identifier,'label':'Marca elegida'})
    app.state.telegram._save_state(owner(app),state)
    chat(document='first')
    saved=app.state.telegram._state(owner(app))
    assert saved['browser_ticket_id']==identifier and saved['browser_choice']['label']=='Marca elegida'
    assert len(extractor.calls)==2


def test_confirmed_replacement_reuses_staged_file_at_storage_limit(billing_client):
    from .test_portal_preparation import prepared
    result,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    ticket=app.state.telegram.workflow.active(owner(app))
    count=app.state.billing.store.count('document',owner(app))
    app.state.billing.settings.billing_max_documents=count+1
    chat(data='xu:'+ticket['id'])
    telegram.files['replacement']=image('yellow');chat(document='replacement')
    staged=app.state.telegram.workflow.active(owner(app))['document_id']
    expiry=app.state.billing.document(owner(app),staged)['_expires']
    assert app.state.telegram.workflow.pending(owner(app)) is None
    assert app.state.billing.resolve_ticket(owner(app),ticket['id'])['document_id']==staged
    assert app.state.billing.store.count('document',owner(app))==count+1
    assert app.state.billing.document(owner(app),staged)['_expires']==expiry
    assert 'xa:' in str(telegram.messages[-1][2]) and 'xg:' not in str(telegram.messages[-1][2])
    chat(data=button(telegram,'xa'))
    assert app.state.billing.store.count('document',owner(app))==count+1
    assert app.state.billing.document(owner(app),staged)['_expires']==expiry


def test_expired_new_file_preserves_request_and_allows_starting_another_without_cancelling(billing_client):
    result,identifier=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    telegram.files['second']=image('green');chat(document='second',caption='Es otro ticket')
    active=app.state.telegram.workflow.active(owner(app))
    pending=active['document_id']
    with app.state.database.connection(True) as conn:
        conn.execute('UPDATE billing_records SET expires=0 WHERE id=?',(pending,))
    chat('/estado')
    assert app.state.telegram.workflow.pending(owner(app)) is None
    assert app.state.telegram.workflow.active(owner(app))['id']==active['id'] and active['id'][:8] in telegram.messages[-1][1]
    assert app.state.billing.resolve_ticket(owner(app),identifier)['telegram_workflow']=='paused'
    assert 'temporal venció' in telegram.messages[-1][1]
    chat('/nuevo')
    assert app.state.telegram.workflow.active(owner(app)) is None


def test_replacing_an_expired_replacement_does_not_collide_with_its_old_lookup(billing_client):
    from .test_portal_preparation import prepared
    result,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    who=owner(app);ticket=app.state.telegram.workflow.active(who)
    expired=app.state.billing.portal_preparation.replace_source(who,ticket['id'],image('yellow'))
    with app.state.database.connection(True) as conn:
        conn.execute('UPDATE billing_records SET expires=0 WHERE id=?',(expired['id'],))
    chat(data='xu:'+ticket['id']);telegram.files['again']=image('yellow');chat(document='again')
    staged=app.state.telegram.workflow.active(who)['document_id']
    assert app.state.telegram.workflow.pending(who) is None
    assert app.state.billing.resolve_ticket(who,ticket['id'])['document_id']==staged
    assert 'xa:' in str(telegram.messages[-1][2]) and len(extractor.calls)==3


def test_replacement_uses_validated_photo_bytes_without_recompressing_it(billing_client):
    import base64,io,random
    from PIL import Image
    from backend.app.billing.documents import inspect_document
    from .test_portal_preparation import prepared
    result,browser,_=prepared(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    who=owner(app);ticket=app.state.telegram.workflow.active(who)
    picture=Image.frombytes('RGB',(96,96),random.Random(42).randbytes(96*96*3))
    original=io.BytesIO();picture.save(original,format='PNG')
    chat(data='xu:'+ticket['id']);telegram.files['detail']=original.getvalue();chat(document='detail')
    staged=app.state.telegram.workflow.active(who)['document_id']
    saved=app.state.billing.document(who,staged)
    assert app.state.billing.resolve_ticket(who,ticket['id'])['document_id']==staged
    assert base64.b64decode(saved['data'])==inspect_document(original.getvalue(),app.state.settings.billing_max_upload_bytes)[1]
    assert 'xa:' in str(telegram.messages[-1][2]) and len(extractor.calls)==3


def test_explicit_csf_upload_takes_priority_over_an_old_receipt_photo_prompt(billing_client):
    result,identifier=first_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    chat(data='xu:'+identifier)
    telegram.files['other-csf']=image('green')
    chat(document='other-csf',caption='/csf')
    received=app.state.billing.document(owner(app),app.state.telegram._state(owner(app))['last_document_id'])
    assert received['purpose']=='csf'
    assert not app.state.telegram._state(owner(app)).get('portal_upload_ticket')
    assert app.state.telegram.workflow.active(owner(app))['id']==identifier
    assert len(extractor.calls)==2 and button(telegram,'an').endswith(received['id'])


@pytest.mark.parametrize('phrase',['Empezar con otra solicitud','Empieza otra solicitud','Quiero iniciar un nuevo ticket'])
def test_new_request_phrase_keeps_previously_sent_email_intact_and_never_requires_cancelling(billing_client,phrase):
    result,identifier=first_ticket(billing_client,billing_live_email_enabled=True)
    client,app,extractor,mail,sat,telegram,chat=result
    chat(data='ve:'+identifier);chat(data='pr:'+identifier);chat(data=button(telegram,'se'))
    delivery=app.state.billing.resolve_ticket(owner(app),identifier)['delivery']
    chat(phrase)
    assert app.state.telegram.workflow.active(owner(app)) is None
    old=app.state.billing.resolve_ticket(owner(app),identifier)
    assert old['delivery']==delivery and old['telegram_workflow']=='paused'
    assert len(mail.sent)==1 and 'nuevo ticket' in telegram.messages[-1][1]
    assert not any(data.startswith('fw:') for _,data in telegram.messages[-1][2])
