import base64
import io
import json

import pytest
from PIL import Image

from backend.app.billing.documents import inspect_document
from backend.app.billing.extraction import DocumentExtractor
from backend.app.billing.models import FiscalExtraction, regime_code
from .billing_fakes import FISCAL, image, pdf
from .fake_model import ScriptedModel, answer
from .test_billing import billing_client
from .test_telegram_agent import button, create, owner
from .test_telegram_profiles import start_draft
from .test_telegram_documents import confirm_profile


@pytest.mark.parametrize('text,expected', [
    ('Régimen Simplificado de Confianza', '626'),
    ('  régimen simplificado de confianza  ', '626'),
    ('Régimen General de Ley Personas Morales', '601'),
    ('Sueldos y Salarios e Ingresos Asimilados a Salarios', '605'),
    ('Personas Físicas con Actividades Empresariales y Profesionales', '612'),
    ('601', '601'), ('601 o 626', None), ('el que más convenga', None), (None, None)])
def test_fiscal_regime_is_an_exact_catalog_equivalence_not_a_guess(text, expected):
    assert regime_code(text) == expected
    assert FiscalExtraction(**(FISCAL | {'fiscal_regime': text})).fiscal_regime == expected


@pytest.mark.parametrize('field,value,entry,expected_prompt', [
    ('rfc', None, FISCAL['rfc'], 'Falta el RFC.'),
    ('name', None, '/dato EMPRESA DEMO SA DE CV', 'Falta el nombre fiscal.'),
    ('postal_code', '0100', '01000', 'Falta el CP fiscal.'),
    ('fiscal_regime', None, 'Régimen General de Ley Personas Morales', 'Falta el régimen.')])
def test_missing_field_is_completed_in_telegram_without_reupload_or_another_ai_call(billing_client, field, value, entry, expected_prompt):
    model = ScriptedModel()
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model)
    extractor.fiscal[field] = value
    start_draft(telegram, chat)
    assert expected_prompt in telegram.messages[-1][1]
    assert 'ilegible' not in telegram.messages[-1][1] and len(telegram.messages[-1][1]) <= 50
    draft_id = app.state.telegram._state(owner(app))['draft_id']
    document_id = app.state.billing.store.get('profile_draft', owner(app), draft_id)['document_id']
    original = app.state.billing.document(owner(app), document_id)
    chat(entry)
    assert telegram.messages[-1][1] == 'Elige tu uso de CFDI.'
    chat('G03')
    chat('cliente@example.com')
    assert FISCAL['rfc'] in telegram.messages[-1][1]
    assert app.state.billing.store.list('profile', owner(app)) == []
    chat(data=button(telegram, 'pf'))
    profile = app.state.billing.store.list('profile', owner(app))[0]
    assert profile[field] == FISCAL[field] and profile['document_id'] == document_id
    assert app.state.billing.document(owner(app), document_id)['sha256'] == original['sha256']
    assert len(extractor.calls) == 1 and telegram.downloads == ['csf']
    assert model.inputs == mail.sent == sat.calls == []


def test_missing_regime_has_owner_bound_buttons_and_never_chooses_it_for_the_user(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    extractor.fiscal['fiscal_regime'] = None
    start_draft(telegram, chat)
    selection = next(data for _, data in telegram.messages[-1][2] if data.endswith(':626'))
    draft_id = app.state.telegram._state(owner(app))['draft_id']
    chat(data=selection, user=456)
    assert app.state.billing.store.get('profile_draft', owner(app), draft_id)['fiscal_regime'] is None
    chat(data=selection)
    assert app.state.billing.store.get('profile_draft', owner(app), draft_id)['fiscal_regime'] == '626'
    assert app.state.billing.store.list('profile', owner(app)) == []
    assert len(extractor.calls) == 1


@pytest.mark.parametrize('caption', [None, '/csf'])
def test_new_pdf_replaces_incomplete_setup_instead_of_becoming_a_ticket(billing_client, caption):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    extractor.fiscal['postal_code'] = None
    start_draft(telegram, chat)
    old_draft = app.state.telegram._state(owner(app))['draft_id']
    extractor.fiscal = FISCAL.copy()
    telegram.files['new-pdf'] = pdf()
    before = len(telegram.messages)
    chat(document='new-pdf', caption=caption, file_name='SAT-7.pdf')
    assert 'CSF recibido' in telegram.messages[-1][1]
    assert all('Ticket guardado' not in text for _, text, _ in telegram.messages[before:])
    assert len(extractor.calls) == 1  # No consentimiento nuevo todavía.
    analyze = button(telegram, 'an')
    source = app.state.billing.document(owner(app), analyze.split(':')[1])
    assert source['purpose'] == 'csf' and source['mime'] == 'application/pdf'
    chat(data=analyze)
    assert telegram.messages[-1][1] == 'Elige tu uso de CFDI.'
    assert app.state.telegram._state(owner(app))['draft_id'] != old_draft
    assert len(extractor.calls) == 2


def test_csf_filename_is_a_routing_hint_without_analysis_or_profile_confirmation(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    telegram.files['pdf'] = pdf()
    chat(document='pdf', file_name='Csf_documento.pdf')
    assert button(telegram, 'an')
    assert app.state.billing.document(owner(app), button(telegram, 'an').split(':')[1])['purpose'] == 'csf'
    assert extractor.calls == [] and app.state.billing.store.list('profile', owner(app)) == []


def test_invalid_fiscal_value_does_not_change_draft_or_reach_ai(billing_client):
    model = ScriptedModel()
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model)
    extractor.fiscal['postal_code'] = None
    start_draft(telegram, chat)
    draft_id = app.state.telegram._state(owner(app))['draft_id']
    chat('/dato 1234')
    assert 'no es válido' in telegram.messages[-1][1]
    assert app.state.billing.store.get('profile_draft', owner(app), draft_id)['postal_code'] is None
    assert model.inputs == []


def test_diagnostic_shows_actual_missing_fields_without_telling_user_to_convert_pdf(billing_client):
    model = ScriptedModel()
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model)
    extractor.fiscal['fiscal_regime'] = None
    start_draft(telegram, chat)
    chat('Por que ilegible?')
    assert telegram.messages[-1][1] == 'Falta el régimen. Elígelo según tu CSF.'
    assert app.state.telegram.agent_context(owner(app))['missing_fiscal_fields'] == ['fiscal_regime']
    assert model.inputs == []


def test_chat_complaint_is_not_silently_used_as_a_missing_fiscal_name(billing_client):
    model = ScriptedModel()
    client, app, extractor, mail, sat, telegram, chat = create(billing_client, model)
    extractor.fiscal['name'] = None
    start_draft(telegram, chat)
    draft_id = app.state.telegram._state(owner(app))['draft_id']
    chat('El PDF ya te lo envié')
    assert app.state.billing.store.get('profile_draft', owner(app), draft_id)['name'] is None


def test_previous_misrouted_pdf_can_be_recovered_without_another_upload(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    extractor.fiscal['fiscal_regime'] = None
    start_draft(telegram, chat)
    billing = app.state.billing
    received = billing.receive_document(owner(app), 'unclassified', pdf())
    data = billing.document(owner(app), received['id'])
    data['intended_purpose'] = 'ticket'  # Estado dejado por la versión anterior.
    billing.store.update('document', owner(app), received['id'], data)
    state = app.state.telegram._state(owner(app))
    state['last_document_id'] = received['id']
    app.state.telegram._save_state(owner(app), state)
    chat('/perfil')
    recover = next(value for label, value in telegram.messages[-1][2] if label == 'Usar último PDF como CSF')
    chat(data=recover, user=456)
    assert billing.document(owner(app), received['id'])['purpose'] == 'unclassified'
    chat(data=recover)
    assert billing.document(owner(app), received['id'])['purpose'] == 'csf' and len(extractor.calls) == 1
    extractor.fiscal = FISCAL.copy()
    chat(data=button(telegram, 'an'))
    assert telegram.messages[-1][1] == 'Elige tu uso de CFDI.'
    assert telegram.downloads == ['csf'] and len(extractor.calls) == 2


def test_using_another_csf_during_profile_update_keeps_same_profile_target(billing_client):
    client, app, extractor, mail, sat, telegram, chat = create(billing_client)
    confirm_profile(telegram, chat)
    profile = app.state.billing.store.list('profile', owner(app))[0]
    chat('quiero actualizar mi perfil')
    chat(data=button(telegram, 'px'))
    telegram.files['partial-csf'] = image('red')
    extractor.fiscal['postal_code'] = None
    chat(document='partial-csf')
    chat(data=button(telegram, 'an'))
    chat(data=button(telegram, 'fc'))
    telegram.files['clear-csf'] = pdf()
    extractor.fiscal = FISCAL | {'postal_code': '02000'}
    chat(document='clear-csf')
    chat(data=button(telegram, 'an'))
    chat(data=button(telegram, 'pf'))
    assert app.state.billing.store.count('profile', owner(app)) == 1
    assert app.state.billing.store.get('profile', owner(app), profile['id'])['postal_code'] == '02000'
    telegram.files['ticket'] = image('green')
    chat(document='ticket')
    assert app.state.billing.document(owner(app), button(telegram, 'an').split(':')[1])['purpose'] == 'ticket'


def test_scanned_pdf_is_forwarded_as_original_native_file_without_requiring_text_layer(billing_client):
    data = io.BytesIO()
    Image.new('RGB', (100, 100), 'white').save(data, format='PDF')
    scanned = data.getvalue()
    mime, content = inspect_document(scanned, 8_388_608)
    model = ScriptedModel([[answer(json.dumps(FISCAL | {'fiscal_regime': 'Régimen General de Ley Personas Morales'}))]])
    client, app, *rest = billing_client(model=model)
    extractor = DocumentExtractor(app.state.service, app.state.settings)
    result = client.portal.call(extractor.extract, 'csf', mime, content)
    file = model.inputs[0][0]['content'][1]
    assert file['type'] == 'input_file'
    assert base64.b64decode(file['file_data'].split(',', 1)[1]) == scanned
    assert result.fiscal_regime == '601' and len(model.inputs) == 1
