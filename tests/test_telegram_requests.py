from datetime import datetime, timedelta, timezone
from functools import partial
from zoneinfo import ZoneInfo

import pytest

from .billing_fakes import FISCAL, image
from .fake_model import ScriptedModel, answer, call
from .test_billing import billing_client
from .test_telegram_agent import button, create, owner
from .test_telegram_documents import confirm_profile
from .test_telegram_workflow import choice


def receipt(app, telegram, chat, *, color='red', folio='TEST-1'):
    app.state.billing.extractor.ticket['folio'] = folio
    telegram.files[color] = image(color)
    chat(document=color)
    if any(data.startswith('fw:') for _,data in telegram.messages[-1][2] or []):
        chat(data=choice(telegram,'pause'))
    analyze = button(telegram, 'an')
    chat(data=analyze)
    return app.state.telegram._state(owner(app))['last_ticket_id'], analyze


def test_same_picture_reuses_ticket_at_analysis_and_storage_limits_without_ai(billing_client):
    model = ScriptedModel()
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model,
        billing_max_daily_extractions=2, billing_max_documents=2)
    confirm_profile(telegram, chat)
    identifier, analyze = receipt(app, telegram, chat)
    profile = app.state.billing.store.list('profile', owner(app))[0]
    chat(document='red')
    assert app.state.telegram._state(owner(app))['last_ticket_id'] == identifier
    assert 'Estado: Datos listos' in telegram.messages[-1][1]
    assert 'No se ha enviado' in telegram.messages[-1][1]
    assert not any(data.startswith('an:') for _, data in telegram.messages[-1][2])
    chat(data=analyze)
    chat('Repetir solicitud')
    assert len(extractor.calls) == 2 and model.inputs == mail.sent == sat.calls == []
    assert app.state.billing.store.list('profile', owner(app)) == [profile]
    assert app.state.billing.store.count('document', owner(app)) == 2
    assert len(app.state.billing.store.list('ticket', owner(app))) == 1


def test_new_picture_same_purchase_opens_original_and_preserves_photo_and_old_buttons(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    confirm_profile(telegram, chat)
    original, _ = receipt(app, telegram, chat)
    telegram.files['copy'] = image('green')
    chat(document='copy')
    duplicate_button = button(telegram, 'an')
    uploaded = duplicate_button.split(':')[1]
    chat(data=duplicate_button)
    assert app.state.telegram._state(owner(app))['last_ticket_id'] == original
    assert 'Estado: Datos listos' in telegram.messages[-1][1]
    duplicate = app.state.billing.document(owner(app), uploaded)
    assert duplicate['duplicate_ticket_id'] == original and duplicate['data']
    assert len(app.state.billing.store.list('ticket', owner(app))) == 1
    calls = len(extractor.calls)
    chat(data=duplicate_button)
    chat(document='copy')
    chat('Repetir solicitud')
    assert len(extractor.calls) == calls and mail.sent == []
    assert app.state.billing.store.count('profile_draft', owner(app)) == 0


def test_restart_and_expired_original_csf_do_not_restart_fiscal_setup(billing_client, tmp_path):
    path = tmp_path / 'resume.sqlite3'
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, path=path)
    confirm_profile(telegram, chat)
    identifier, _ = receipt(app, telegram, chat)
    profile = app.state.billing.store.list('profile', owner(app))[0]
    with app.state.database.connection() as conn:
        conn.execute('UPDATE billing_records SET expires=0 WHERE id=?', (profile['document_id'],))
    _, restarted, extraction2, mail2, sat2, telegram2, chat2 = create(billing_client, path=path)
    chat2.sequence = 4000
    telegram2.files['again'] = image('red')
    chat2(document='again')
    chat2('Repetir solicitud')
    assert restarted.state.telegram._state(owner(restarted))['last_ticket_id'] == identifier
    assert restarted.state.billing.store.list('profile', owner(restarted)) == [profile]
    assert extraction2.calls == mail2.sent == sat2.calls == []
    assert 'Estado: Datos listos' in telegram2.messages[-1][1]


def test_stale_csf_buttons_and_draft_cannot_restart_confirmed_profile(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    confirm_profile(telegram, chat)
    billing, bridge = app.state.billing, app.state.telegram
    profile = billing.store.list('profile', owner(app))[0]
    # Simular el borrador recreado por una versión anterior desde la CSF original.
    stale = billing.store.add('profile_draft', owner(app), FISCAL | {
        'document_id': profile['document_id'], 'replace_profile_id': None})
    state = bridge._state(owner(app))
    state.update(draft_id=stale['id'], csf_document_id=profile['document_id'],expected_upload='csf')
    bridge._save_state(owner(app), state)
    chat(data='an:' + profile['document_id'])
    assert telegram.messages[-1][1] == 'Perfil listo. Envía tu ticket.'
    chat('/perfil')
    assert telegram.messages[-1][1] == 'Perfil listo. Envía tu ticket.'
    assert len(extractor.calls) == 1
    assert billing.store.list('profile', owner(app)) == [profile]
    telegram.files['next-ticket']=image('yellow')
    chat(document='next-ticket')
    assert billing.document(owner(app),button(telegram,'an').split(':')[1])['purpose']=='ticket'


def test_ticket_error_tool_opens_ticket_instead_of_csf_flow(billing_client):
    model = ScriptedModel()
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model)
    confirm_profile(telegram, chat)
    receipt(app, telegram, chat)
    model.outputs = [[call('analyze_ticket', identifier='00000000-0000-4000-8000-000000000000')],
        [answer('Debes subir tu CSF otra vez.')]]
    chat('Retoma el trámite que teníamos antes')
    assert 'Estado: Datos listos' in telegram.messages[-1][1]
    assert 'subir tu CSF' not in telegram.messages[-1][1]
    assert len(extractor.calls) == 2


def test_model_cannot_reopen_confirmed_csf_from_old_history(billing_client):
    model = ScriptedModel()
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model)
    confirm_profile(telegram, chat)
    profile = app.state.billing.store.list('profile', owner(app))[0]
    model.outputs = [[call('process_fiscal_certificate', identifier=profile['document_id'])],
        [answer('Falta confirmar tu CSF.')]]
    chat('Revisa los datos que guardamos')
    assert telegram.messages[-1][1] == 'Perfil listo. Envía tu ticket.'
    assert app.state.billing.store.count('profile_draft', owner(app)) == 0
    assert len(extractor.calls) == 1


def test_pending_email_is_reviewed_not_repeated_or_claimed_sent(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    confirm_profile(telegram, chat)
    identifier, _ = receipt(app, telegram, chat)
    billing = app.state.billing
    billing.verify_billing_email(owner(app), identifier, confirmed=True)
    approval = billing.request_email_send_approval(owner(app), identifier)
    chat('Repetir solicitud')
    assert 'Pendiente de tu confirmación' in telegram.messages[-1][1]
    assert 'Aún no se envió' in telegram.messages[-1][1]
    assert button(telegram, 'er') == 'er:' + approval['id']
    assert all(not data.startswith(('ec:', 'pr:', 'vc:')) for _, data in telegram.messages[-1][2])
    chat(data=button(telegram, 'er'))
    assert 'facturacion@example.com' in telegram.messages[-1][1]
    assert mail.sent == []


def test_uncertain_email_blocks_repeat_and_does_not_claim_unsent(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    confirm_profile(telegram, chat)
    identifier, _ = receipt(app, telegram, chat)
    app.state.billing.store.add('email_approval', owner(app), {'ticket_id': identifier}, state='unknown')
    chat('Repetir solicitud')
    text, controls = telegram.messages[-1][1:]
    assert 'Envío incierto' in text and 'No se ha enviado' not in text
    assert all(not data.startswith(('ec:', 'pr:', 'vc:', 'er:')) for _, data in controls)
    assert mail.sent == []


def test_analysis_limit_names_counter_and_local_reset_and_keeps_pending_ticket(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, billing_max_daily_extractions=1)
    confirm_profile(telegram, chat)
    telegram.files['new'] = image('red')
    chat(document='new')
    analyze = button(telegram, 'an')
    chat(data=analyze)
    text = telegram.messages[-1][1]
    assert 'Análisis de archivos: 1/1' in text and 'America/Mexico_City' in text
    now = datetime.now(timezone.utc)
    reset = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(ZoneInfo('America/Mexico_City'))
    assert reset.strftime('%d/%m %H:%M') in text
    chat('/estado')
    assert 'Estado:' in telegram.messages[-1][1] and 'CSF' not in telegram.messages[-1][1]
    assert app.state.billing.document(owner(app), analyze.split(':')[1])['data']
    assert len(extractor.calls) == 1
    # El rechazo no crea otro consumo ni requiere volver a subir el ticket.
    app.state.billing.settings.billing_max_daily_extractions = 2
    chat(data=analyze)
    assert 'Estado: Datos listos' in telegram.messages[-1][1] and len(extractor.calls) == 2


def test_read_only_status_and_limits_remain_available_when_message_budget_exhausted(billing_client):
    model = ScriptedModel()
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model,
        billing_max_daily_telegram_updates=10)
    confirm_profile(telegram, chat)
    identifier, _ = receipt(app, telegram, chat)
    while app.state.telegram.requests.usage(owner(app))['updates'] < 10:
        chat('/documentos')
    response, payload = chat('Calcula 5+5')
    assert response.json()['rate_limited']
    assert 'Mensajes y acciones: 10/10' in telegram.messages[-1][1]
    chat('/estado')
    assert 'Estado: Datos listos' in telegram.messages[-1][1]
    chat('/limites')
    assert 'Análisis de archivos: 2/10' in telegram.messages[-1][1]
    _, payload = chat(data='ts:' + identifier)
    before = len(telegram.messages)
    duplicate = client.post('/integrations/telegram/webhook', json=payload,
        headers={'X-Telegram-Bot-Api-Secret-Token': 'x'*40})
    assert duplicate.json()['duplicate'] and len(telegram.messages) == before
    assert model.inputs == mail.sent == sat.calls == []
    assert app.state.telegram.requests.usage(owner(app))['updates'] == 10


def test_tickets_menu_and_status_buttons_enforce_owner(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    confirm_profile(telegram, chat)
    first, _ = receipt(app, telegram, chat)
    second, _ = receipt(app, telegram, chat, color='green', folio='TEST-2')
    chat('/tickets')
    assert len([data for _,data in telegram.messages[-1][2] if data.startswith('ts:')]) == 2
    chat(data='ts:' + first)
    chat(data=choice(telegram,'pause'))
    assert 'TEST-1' in telegram.messages[-1][1]
    chat(data='ts:' + second, user=456)
    assert 'no encontrado' in telegram.messages[-1][1]
    assert 'TEST-2' not in telegram.messages[-1][1] and FISCAL['rfc'] not in telegram.messages[-1][1]


@pytest.mark.parametrize('options,expected', [({'max_daily_runs':1}, 'Chat IA: 1/1'),
    ({'max_conversation_turns':1}, '/nuevo_chat abre otro')])
def test_chat_limit_explains_the_correct_budget_and_preserves_profile(billing_client,options,expected):
    model=ScriptedModel([[answer('Hola.')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,**options)
    confirm_profile(telegram,chat)
    chat('Hola')
    chat('Otra pregunta')
    assert expected in telegram.messages[-1][1]
    chat('/perfil')
    assert telegram.messages[-1][1]=='Perfil listo. Envía tu ticket.'
    assert len(model.inputs)==1 and len(extractor.calls)==1


def test_sent_request_shows_response_control_and_never_resends_or_polls_on_repeat(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,billing_live_email_enabled=True)
    confirm_profile(telegram,chat)
    identifier,_=receipt(app,telegram,chat)
    billing=app.state.billing
    billing.verify_billing_email(owner(app),identifier,confirmed=True)
    approval=billing.request_email_send_approval(owner(app),identifier)
    client.portal.call(partial(billing.send_invoice_request_email,owner(app),approval['id'],confirmed=True))
    chat('Repetir solicitud')
    assert 'Esperando al comercio' in telegram.messages[-1][1]
    assert 'Solicitud enviada' in telegram.messages[-1][1]
    assert button(telegram,'tr')=='tr:'+identifier
    assert len(mail.sent)==1 and mail.fetch_calls==0 and sat.calls==[]


def test_duplicate_photo_is_removed_with_its_ticket_only_after_explicit_deletion(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    confirm_profile(telegram,chat)
    identifier,_=receipt(app,telegram,chat)
    telegram.files['copy']=image('green')
    chat(document='copy')
    action=button(telegram,'an')
    chat(data=action)
    app.state.billing.delete_ticket(owner(app),identifier)
    assert app.state.billing.store.count('document',owner(app))==1  # Solo la CSF.
    assert app.state.billing.store.list('ticket',owner(app))==[]


def quota_blocked_ticket(factory):
    result=create(factory,billing_max_daily_extractions=1)
    client,app,extractor,mail,sat,telegram,chat=result
    confirm_profile(telegram,chat)
    identifier,analyze=receipt(app,telegram,chat)
    previous=(datetime.now(timezone.utc)-timedelta(days=1)).date().isoformat()
    return result,identifier,analyze,previous


def test_status_after_daily_reset_offers_saved_ticket_without_showing_stale_limit(billing_client):
    result,identifier,analyze,previous=quota_blocked_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    with app.state.database.connection() as conn:
        conn.execute('UPDATE billing_extractions SET day=? WHERE owner=?',(previous,owner(app)))
    chat('/limites')
    assert 'Análisis de archivos: 0/1' in telegram.messages[-1][1]
    chat('/estado')
    text=telegram.messages[-1][1]
    assert 'Estado: Pendiente de análisis' in text and 'Hay cupo disponible' in text
    assert 'Alcanzaste el límite' not in text and button(telegram,'an')==analyze
    assert len(extractor.calls)==1 and mail.sent==sat.calls==[]
    # La vista no borra el bloqueo anterior del historial ni reinicia cuotas.
    stored=app.state.billing.store.get('ticket',owner(app),identifier)
    assert stored['analysis_error']['code']=='billing_daily_limit'
    assert app.state.telegram.requests.usage(owner(app))['analyses']==0
    chat(data=analyze)
    assert 'Estado: Datos listos' in telegram.messages[-1][1]
    assert app.state.telegram.requests.usage(owner(app))['analyses']==1 and len(extractor.calls)==2
    chat(data=analyze)
    assert len(extractor.calls)==2


def test_currently_exhausted_analysis_budget_hides_analysis_button_and_shows_reset(billing_client):
    result,identifier,analyze,previous=quota_blocked_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    chat('/estado')
    text,buttons=telegram.messages[-1][1:]
    assert 'Estado: En espera de cupo' in text
    assert 'Análisis: 1/1' in text and 'Se restablece' in text
    assert all(not value.startswith(('an:','ra:')) for _,value in buttons)
    assert button(telegram,'ts')=='ts:'+identifier and len(extractor.calls)==1


def test_daily_reset_does_not_allow_repeating_a_previous_file_attempt(billing_client):
    result,identifier,analyze,previous=quota_blocked_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    document_id=analyze.split(':')[1]
    with app.state.database.connection() as conn:
        conn.execute('UPDATE billing_extractions SET day=? WHERE owner=?',(previous,owner(app)))
        conn.execute('INSERT INTO billing_extractions VALUES(?,?,?,?)',(owner(app),previous,document_id,'failed'))
    chat('/estado')
    text,buttons=telegram.messages[-1][1:]
    assert 'Estado: Análisis ya intentado' in text and 'Hay cupo disponible' not in text
    assert all(not value.startswith(('an:','ra:')) for _,value in buttons)
    assert len(extractor.calls)==1 and app.state.telegram.requests.usage(owner(app))['analyses']==0
    chat(data=analyze)  # Un botón viejo tampoco elude el registro persistente.
    assert len(extractor.calls)==1


def test_expired_ticket_after_reset_requests_only_ticket_copy_and_preserves_profile(billing_client):
    result,identifier,analyze,previous=quota_blocked_ticket(billing_client)
    client,app,extractor,mail,sat,telegram,chat=result
    profile=app.state.billing.store.list('profile',owner(app))[0]
    with app.state.database.connection() as conn:
        conn.execute('UPDATE billing_extractions SET day=? WHERE owner=?',(previous,owner(app)))
        conn.execute('UPDATE billing_records SET expires=0 WHERE id=?',(analyze.split(':')[1],))
    chat('/estado')
    text,buttons=telegram.messages[-1][1:]
    assert 'Envía otra copia del ticket' in text and 'Tu perfil se conserva' in text
    assert 'Alcanzaste el límite' not in text
    assert all(not value.startswith(('an:','ra:')) for _,value in buttons)
    assert app.state.billing.store.list('profile',owner(app))==[profile] and len(extractor.calls)==1
