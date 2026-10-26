from functools import partial

import pytest

from backend.app.errors import AppError
from .billing_fakes import FISCAL, image
from .fake_model import ScriptedModel, answer, call
from .test_billing import billing_client
from .test_telegram_agent import button, create, owner


def confirm_profile(telegram,chat):
    telegram.files['csf']=image('blue')
    chat(document='csf',caption='/csf')
    chat(data=button(telegram,'an'))
    chat('G03')
    chat('cliente@example.com')
    chat(data=button(telegram,'pf'))


def test_unlabelled_file_is_not_assumed_to_be_csf_or_sent_to_ai(billing_client):
    model=ScriptedModel()
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model)
    telegram.files['receipt']=image()
    chat(document='receipt')
    assert button(telegram,'dc') and button(telegram,'dt')
    documents=app.state.billing.store.list('document',owner(app))
    assert len(documents)==1 and documents[0]['purpose']=='unclassified'
    assert extractor.calls==model.inputs==mail.sent==[]


def test_ticket_before_profile_is_kept_and_resumed_without_resubmission(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    telegram.files['receipt']=image('yellow')
    chat(document='receipt')
    source_id=button(telegram,'dt').split(':')[1]
    before=app.state.billing.document(owner(app),source_id)
    chat(data=button(telegram,'dt'))
    assert 'No necesitas volver a subirlo' in telegram.messages[-1][1]
    assert extractor.calls==[]
    confirm_profile(telegram,chat)
    assert 'Tickets guardados' in telegram.messages[-1][1]
    chat(data=button(telegram,'dt'))
    assert button(telegram,'an').endswith(source_id)
    chat(data=button(telegram,'an'))
    record=app.state.billing.document(owner(app),source_id)
    assert record['purpose']=='ticket' and record['sha256']==before['sha256'] and record['data']==before['data']
    assert record['profile_id']==app.state.telegram._state(owner(app))['active_profile']
    assert telegram.downloads==['receipt','csf']
    assert [purpose for purpose,_ in extractor.calls]==['csf','ticket']
    assert app.state.billing.store.count('document',owner(app))==2
    assert mail.sent==[]


def test_upload_defaults_to_ticket_after_profile_is_confirmed(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('green')
    chat(document='receipt')
    identifier=button(telegram,'an').split(':')[1]
    assert app.state.billing.document(owner(app),identifier)['purpose']=='ticket'
    assert all(not data.startswith(('dc:','dt:')) for _,data in telegram.messages[-1][2])
    assert len(extractor.calls)==1


def test_explicit_csf_caption_still_adds_another_certificate(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    confirm_profile(telegram,chat)
    telegram.files['another-csf']=image('green')
    chat(document='another-csf',caption='/csf')
    identifier=button(telegram,'an').split(':')[1]
    assert app.state.billing.document(owner(app),identifier)['purpose']=='csf'
    assert len(extractor.calls)==1


def test_legacy_wrong_csf_can_be_corrected_by_text_and_recovers_original_bytes(billing_client):
    model=ScriptedModel()
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model)
    billing=app.state.billing
    legacy=billing.receive_document(owner(app),'csf',image('yellow'))
    extractor.fiscal=dict(FISCAL,is_fiscal_certificate=False)
    with pytest.raises(AppError,match='not_fiscal_certificate'):
        client.portal.call(partial(billing.process_fiscal_certificate,owner(app),legacy['id'],consent=True))
    original=billing.document(owner(app),legacy['id'])
    state=app.state.telegram._state(owner(app))
    state['last_document_id']=legacy['id']
    app.state.telegram._save_state(owner(app),state)
    chat('Es un ticket')
    assert model.inputs==[] and len(extractor.calls)==1
    assert 'Ticket guardado' in telegram.messages[-1][1]
    extractor.fiscal=FISCAL.copy()
    confirm_profile(telegram,chat)
    chat(data=button(telegram,'dt'))
    corrected_id=button(telegram,'an').split(':')[1]
    assert corrected_id!=legacy['id']
    corrected=billing.document(owner(app),corrected_id)
    assert corrected['data']==original['data'] and corrected['sha256']==original['sha256']
    assert corrected['extracted'] is None and corrected['analysis_consent'] is False
    assert billing.document(owner(app),legacy['id'])['extracted']['is_fiscal_certificate'] is False
    with app.state.database.connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM billing_extractions WHERE owner=? AND document_id=?',
            (owner(app),legacy['id'])).fetchone()[0]==1
    chat(data='an:'+legacy['id'])
    assert len(extractor.calls)==2  # Solo la CSF nueva se analizó tras el intento original.
    chat(data='an:'+corrected_id)
    assert [purpose for purpose,_ in extractor.calls]==['csf','csf','ticket']
    assert billing.document(owner(app),corrected_id)['purpose']=='ticket'
    assert telegram.downloads==['csf']  # El ticket antiguo no se descargó ni subió de nuevo.


def test_failed_csf_analysis_offers_type_correction_without_repeating_extraction(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    extractor.fiscal=dict(FISCAL,is_fiscal_certificate=False)
    telegram.files['receipt']=image()
    chat(document='receipt',caption='/csf')
    analyze=button(telegram,'an')
    chat(data=analyze)
    assert button(telegram,'dt') and 'sigue guardado' in telegram.messages[-1][1]
    chat(data=analyze)
    assert len(extractor.calls)==1


@pytest.mark.parametrize('prefix',['dc','dt','dv'])
def test_document_type_controls_are_bound_to_their_owner(billing_client,prefix):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    telegram.files['receipt']=image()
    chat(document='receipt')
    identifier=button(telegram,'dt').split(':')[1]
    chat(data=prefix+':'+identifier,user=456)
    assert 'no encontrado' in telegram.messages[-1][1]
    assert app.state.billing.document(owner(app),identifier)['purpose']=='unclassified'
    assert extractor.calls==[]


def test_explicit_ticket_caption_does_not_analyze_or_lose_file_without_profile(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    telegram.files['receipt']=image()
    chat(document='receipt',caption='/ticket')
    assert 'Ticket guardado' in telegram.messages[-1][1] and extractor.calls==[]
    chat('/documentos')
    assert button(telegram,'dv')
    chat(data=button(telegram,'dv'))
    assert button(telegram,'dc') and button(telegram,'dt')


def test_model_receives_actual_profile_and_document_state_without_fiscal_fields(billing_client):
    class InspectingModel(ScriptedModel):
        async def get_response(self,system_instructions,*args,**kwargs):
            self.last_policy=system_instructions
            return await super().get_response(system_instructions,*args,**kwargs)

    model=InspectingModel([[answer('El archivo está guardado; falta confirmar el perfil.')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model)
    telegram.files['receipt']=image()
    chat(document='receipt',caption='/ticket')
    identifier=app.state.telegram._state(owner(app))['last_document_id']
    chat('Te mencioné mi régimen fiscal, ¿ya confirmaste mi perfil?')
    assert '"profile_confirmed": false' in model.last_policy
    assert identifier in model.last_policy and '"awaiting_profile": true' in model.last_policy
    assert FISCAL['rfc'] not in model.last_policy and FISCAL['name'] not in model.last_policy


def test_agent_can_review_saved_file_without_asking_for_another_upload(billing_client):
    model=ScriptedModel()
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model)
    telegram.files['receipt']=image()
    chat(document='receipt')
    identifier=button(telegram,'dt').split(':')[1]
    model.outputs=[[call('review_received_document',identifier=identifier)],[answer('Revisa el tipo del archivo guardado.')]]
    chat('El documento que ya te pasé es un recibo; revísalo')
    assert button(telegram,'dt').endswith(identifier)
    assert len(telegram.downloads)==1 and extractor.calls==[]


def test_classification_is_idempotent_and_does_not_extend_expiry(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    billing=app.state.billing
    source=billing.receive_document(owner(app),'unclassified',image())
    first=billing.classify_document(owner(app),source['id'],'csf')
    second=billing.classify_document(owner(app),source['id'],'csf')
    assert first['id']==second['id']==source['id'] and second['duplicate'] is True
    assert first['expires']==source['expires'] and billing.store.count('document',owner(app))==1


def test_unclassified_purpose_is_not_exposed_as_a_web_upload_option(billing_client):
    client,app,*_=create(billing_client)
    response=client.post('/api/billing/documents?purpose=unclassified',content=image())
    assert response.status_code==422
    assert app.state.billing.store.count('document','owner')==0


def test_registered_profile_survives_new_chat_and_expiry_of_original_csf(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    confirm_profile(telegram,chat)
    profile=app.state.billing.store.list('profile',owner(app))[0]
    with app.state.database.connection() as connection:
        connection.execute('UPDATE billing_records SET expires=0 WHERE id=?',(profile['document_id'],))
    app.state.billing.store.purge()
    chat('/nuevo')
    state=app.state.telegram._state(owner(app))
    state['active_profile']=None  # Se recupera la única elección posible sin volver a pedir CSF.
    app.state.telegram._save_state(owner(app),state)
    telegram.files['receipt']=image('red')
    chat(document='receipt')
    identifier=button(telegram,'an').split(':')[1]
    assert app.state.billing.document(owner(app),identifier)['profile_id']==profile['id']
    assert app.state.telegram._state(owner(app))['active_profile']==profile['id']
    assert app.state.telegram.agent_context(owner(app))['csf_received'] is True
    assert len(extractor.calls)==1


def test_ticket_after_csf_analysis_asks_only_for_missing_profile_step(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    telegram.files.update(csf=image('blue'),receipt=image('yellow'))
    chat(document='csf',caption='/csf')
    chat(data=button(telegram,'an'))
    chat(document='receipt')
    assert 'Tu CSF ya está recibida' in telegram.messages[-1][1]
    assert '/usos' in telegram.messages[-1][1]
    assert 'Envía tu CSF' not in telegram.messages[-1][1]
    assert len(extractor.calls)==1
    state=app.state.telegram._state(owner(app))
    assert app.state.billing.document(owner(app),state['last_document_id'])['intended_purpose']=='ticket'


def test_csf_received_without_analysis_is_not_requested_again(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    telegram.files.update(csf=image('blue'),receipt=image('yellow'))
    chat(document='csf',caption='/csf')
    csf_id=button(telegram,'an').split(':')[1]
    chat(document='receipt')
    assert button(telegram,'an').endswith(csf_id)
    assert 'comparte con OpenAI' in telegram.messages[-1][1]
    assert extractor.calls==[]
    assert app.state.telegram.agent_context(owner(app))['csf_received'] is True


def test_automatic_ticket_default_never_reuses_another_users_profile(billing_client):
    client,app,extractor,mail,sat,telegram,chat=create(billing_client)
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('yellow')
    chat(document='receipt',user=456)
    assert button(telegram,'dc') and button(telegram,'dt')
    assert app.state.billing.store.list('profile',owner(app,456))==[]
