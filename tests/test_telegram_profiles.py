import pytest

from backend.app.replies import brief_reply
from .billing_fakes import FISCAL, image
from .fake_model import ScriptedModel, answer, call
from .test_billing import billing_client
from .test_telegram_agent import button, create, owner
from .test_telegram_documents import confirm_profile


def start_draft(telegram, chat):
    telegram.files['csf'] = image('blue')
    chat(document='csf', caption='/csf')
    chat(data=button(telegram, 'an'))


def choose_g03(telegram, chat):
    controls = telegram.messages[-1][2]
    chat(data=next(value for _, value in controls if value.endswith(':G03')))


def test_first_message_and_profile_steps_are_short_and_work_without_web_or_ai_chat(billing_client):
    model = ScriptedModel()
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model)
    chat('/start')
    assert len(telegram.messages[-1][1]) <= 50
    start_draft(telegram, chat)
    assert len(telegram.messages[-1][1]) <= 50
    assert len(telegram.messages[-1][2]) > 10  # Catálogo, elección del usuario.
    choose_g03(telegram, chat)
    assert len(telegram.messages[-1][1]) <= 50
    chat('mi correo es cliente@example.com')
    review = telegram.messages[-1][1]
    assert all(value in review for value in (FISCAL['rfc'], FISCAL['name'], 'G03', 'cliente@example.com'))
    assert app.state.billing.store.list('profile', owner(app)) == []
    chat('Sí')  # Abre revisión; no sustituye al botón de confirmación.
    assert app.state.billing.store.list('profile', owner(app)) == []
    chat(data=button(telegram, 'pf'))
    assert len(telegram.messages[-1][1]) <= 50
    assert app.state.billing.store.list('profile', owner(app))[0]['email'] == 'cliente@example.com'
    assert model.inputs == mail.sent == sat.calls == []
    assert len(extractor.calls) == 1
    assert client.get('/api/billing/config').json()['profiles'] == []


def test_fiscal_status_question_opens_missing_step_instead_of_generic_offer(billing_client):
    model = ScriptedModel()
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model)
    start_draft(telegram, chat)
    chat('Tienes los datos para facturar?')
    assert telegram.messages[-1][1] == 'Elige tu uso de CFDI.'
    choose_g03(telegram, chat)
    chat('Si')
    assert telegram.messages[-1][1] == 'Escribe tu correo para recibir facturas.'
    assert not any('¿Quieres' in text or 'portal' in text for _, text, _ in telegram.messages)
    assert model.inputs == [] and len(extractor.calls) == 1


def test_confirmed_profile_status_does_not_create_another_profile_or_draft(billing_client):
    model = ScriptedModel()
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model)
    confirm_profile(telegram, chat)
    chat('Tienes los datos para facturar?')
    assert telegram.messages[-1][1] == 'Perfil listo. Envía tu ticket.'
    assert app.state.billing.store.count('profile', owner(app)) == 1
    assert app.state.billing.store.count('profile_draft', owner(app)) == 0
    assert len(extractor.calls) == 1 and model.inputs == []


def test_expired_draft_resumes_cached_csf_without_second_analysis_or_upload(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    start_draft(telegram, chat)
    old = app.state.telegram._state(owner(app))['draft_id']
    with app.state.database.connection() as connection:
        connection.execute('UPDATE billing_records SET expires=0 WHERE id=?', (old,))
    chat('/perfil')
    assert button(telegram, 'cu') and len(extractor.calls) == 1
    assert app.state.telegram._state(owner(app))['draft_id'] != old
    assert telegram.downloads == ['csf']


def test_email_update_requires_versioned_button_and_preserves_existing_profile_until_confirmed(billing_client):
    model = ScriptedModel()
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model)
    confirm_profile(telegram, chat)
    initial = app.state.billing.store.list('profile', owner(app))[0]
    chat('mi correo es nuevo@example.com')
    stale = button(telegram, 'pf')
    assert app.state.billing.store.get('profile', owner(app), initial['id'])['email'] == 'cliente@example.com'
    chat('otro@example.com')
    current = button(telegram, 'pf')
    assert current != stale
    chat(data=stale)
    assert 'cambiaron' in telegram.messages[-1][1]
    assert app.state.billing.store.get('profile', owner(app), initial['id'])['email'] == 'cliente@example.com'
    chat(data=current, user=456)
    assert app.state.billing.store.get('profile', owner(app), initial['id'])['email'] == 'cliente@example.com'
    chat(data=current)
    updated = app.state.billing.store.get('profile', owner(app), initial['id'])
    assert updated['email'] == 'otro@example.com' and updated['_version'] > initial['_version']
    assert all(updated[key] == initial[key] for key in ('rfc', 'name', 'postal_code', 'fiscal_regime', 'cfdi_use'))
    assert app.state.billing.store.count('profile', owner(app)) == 1
    chat(data=current)
    assert app.state.billing.store.get('profile', owner(app), initial['id'])['_version'] == updated['_version']
    assert model.inputs == [] and len(extractor.calls) == 1


def test_email_can_be_updated_after_original_file_expires(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    confirm_profile(telegram, chat)
    profile = app.state.billing.store.list('profile', owner(app))[0]
    with app.state.database.connection() as connection:
        connection.execute('UPDATE billing_records SET expires=0 WHERE id=?', (profile['document_id'],))
    chat('correo: nuevo@example.com')
    chat(data=button(telegram, 'pf'))
    assert app.state.billing.store.get('profile', owner(app), profile['id'])['email'] == 'nuevo@example.com'
    assert len(extractor.calls) == 1


def test_inline_edit_and_cancel_keep_old_profile_and_usage(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    confirm_profile(telegram, chat)
    profile = app.state.billing.store.list('profile', owner(app))[0]
    chat('/perfil')
    chat(data=button(telegram, 'pe'))
    chat(data=button(telegram, 'pu'))
    chat(data=next(value for _, value in telegram.messages[-1][2] if value.endswith(':S01')))
    assert 'S01' in telegram.messages[-1][1]
    chat(data=button(telegram, 'pd'))
    assert app.state.billing.store.get('profile', owner(app), profile['id']) == profile


def test_replacing_csf_updates_same_profile_only_after_inline_consent_and_confirmation(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    confirm_profile(telegram, chat)
    profile = app.state.billing.store.list('profile', owner(app))[0]
    chat('quiero actualizar mi perfil')
    chat(data=button(telegram, 'px'))
    extractor.fiscal = dict(FISCAL, postal_code='02000')
    telegram.files['new-csf'] = image('red')
    chat(document='new-csf')
    assert len(extractor.calls) == 1
    chat(data=button(telegram, 'an'))
    assert '02000' in telegram.messages[-1][1] and 'G03' in telegram.messages[-1][1]
    assert app.state.billing.store.get('profile', owner(app), profile['id'])['postal_code'] == '01000'
    chat(data=button(telegram, 'pf'))
    updated = app.state.billing.store.get('profile', owner(app), profile['id'])
    assert updated['postal_code'] == '02000' and updated['email'] == profile['email']
    assert app.state.billing.store.count('profile', owner(app)) == 1
    previous_drafts = app.state.billing.store.count('profile_draft', owner(app))
    chat('/perfil')
    assert telegram.messages[-1][1] == 'Perfil listo. Envía tu ticket.'
    assert app.state.billing.store.count('profile_draft', owner(app)) == previous_drafts


def test_previous_pending_cfdi_choice_survives_upgrade(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    start_draft(telegram, chat)
    state = app.state.telegram._state(owner(app))
    state['cfdi_use'] = 'G03'  # Formato usado por la versión anterior.
    app.state.telegram._save_state(owner(app), state)
    chat('/perfil')
    assert telegram.messages[-1][1] == 'Escribe tu correo para recibir facturas.'
    chat('cliente@example.com')
    chat(data=button(telegram, 'pf'))
    assert app.state.billing.store.list('profile', owner(app))[0]['cfdi_use'] == 'G03'


@pytest.mark.parametrize('prefix', ['pe', 'pp', 'px'])
def test_profile_edit_buttons_cannot_read_or_change_another_owner(billing_client, prefix):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    confirm_profile(telegram, chat)
    profile = app.state.billing.store.list('profile', owner(app))[0]
    chat(data=prefix + ':' + profile['id'], user=456)
    assert FISCAL['rfc'] not in telegram.messages[-1][1]
    assert app.state.billing.store.count('profile_draft', owner(app, 456)) == 0


def test_model_tool_opens_profile_controls_without_web_redirect_or_filler(billing_client):
    model = ScriptedModel([[call('review_fiscal_setup')], [answer('¿Quieres que te ayude a entrar al portal?')]])
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model)
    start_draft(telegram, chat)
    before = len(telegram.messages)
    chat('Retoma el alta con mis datos guardados')
    assert len(telegram.messages) == before + 1
    assert telegram.messages[-1][1] == 'Elige tu uso de CFDI.'
    assert len(model.inputs) == 2


def test_chat_limits_verbose_output_without_repeating_model_call(billing_client):
    class InspectingModel(ScriptedModel):
        async def get_response(self, system_instructions, input, model_settings, *args, **kwargs):
            self.limit = model_settings.max_tokens
            return await super().get_response(system_instructions, input, model_settings, *args, **kwargs)

    model = InspectingModel([[answer('El total es 575 pesos. ' + 'Explicación extensa. ' * 30 + '¿Quieres que te ayude en algo más?')]])
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model)
    chat('Dame el total')
    assert len(telegram.messages[-1][1]) <= 160 and '575' in telegram.messages[-1][1]
    assert '¿Quieres' not in telegram.messages[-1][1]
    assert model.limit == 100 and len(model.inputs) == 1


def test_shortening_preserves_specific_question_and_decimal_result():
    assert brief_reply('Cada persona paga 287.50 pesos. ¿Te ayudo?') == 'Cada persona paga 287.50 pesos.'
    assert brief_reply('¿Qué correo quieres usar?') == '¿Qué correo quieres usar?'
    assert len(brief_reply('palabra ' * 100)) <= 160


def test_legacy_profile_confirm_button_requires_a_new_versioned_review(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    start_draft(telegram, chat)
    chat('G03')
    chat('cliente@example.com')
    draft = app.state.telegram._state(owner(app))['draft_id']
    chat(data='pf:' + draft)
    assert app.state.billing.store.count('profile', owner(app)) == 0
    chat(data=button(telegram, 'pf'))
    assert app.state.billing.store.count('profile', owner(app)) == 1
