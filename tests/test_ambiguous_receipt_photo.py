"""Una foto ambigua espera una decisión; una foto solicitada conserva su contexto."""
import time
from functools import partial

import pytest

from backend.app.billing.extraction import FieldExtraction
from .billing_fakes import image
from .test_billing import billing_client
from .test_official_portal_discovery import Modern, modern
from .test_portal_preparation import ScopedExtractor, prepared
from .test_portal_submission import PortalPages, enable_transactions
from .test_prepared_approval_quota import fill_daily_quota
from .test_telegram_agent import button, owner
from .test_telegram_workflow import choice


def receive_ambiguous(factory, *, maximum=None, last_update=False):
    extractor = Modern(); extractor.found = None; extractor.ticket['total'] = None
    configured = partial(factory,billing_max_documents=maximum) if maximum else factory
    values, _ = modern(configured,extractor)
    _, app, extractor, _, _, telegram, chat = values
    chat(data=button(telegram,'an'))
    previous = app.state.telegram.workflow.active(owner(app))
    calls = list(extractor.calls)
    if last_update:fill_daily_quota(app,maximum=99)
    telegram.files['unknown'] = image('yellow'); chat(document='unknown')
    return values, previous, calls


def test_unknown_photo_is_saved_without_replacing_or_reading_and_asks_two_choices(billing_client):
    values, previous, calls = receive_ambiguous(billing_client)
    _, app, extractor, mail, sat, telegram, _ = values
    current = app.state.telegram.workflow.active(owner(app))
    assert current['id']==previous['id'] and current['document_id']==previous['document_id']
    assert extractor.calls==calls and mail.sent==sat.calls==[]
    pending = app.state.telegram.workflow.pending(owner(app))
    assert pending is not None and pending['from_ticket_id']==previous['id']
    assert time.time() < pending['_expires'] <= time.time()+1800
    document = app.state.billing.document(owner(app),pending['document_id'])
    assert document['purpose']=='unclassified' and document['extracted'] is None and not document['analysis_consent']
    assert app.state.billing.store.count('document',owner(app))==3
    assert {label for label,_ in telegram.messages[-1][2]}=={'Corrige este mismo ticket','Es otro ticket'}
    assert all(len(data.encode())<=64 for _,data in telegram.messages[-1][2])


def test_same_choice_replaces_source_once_even_when_the_photo_consumed_update_100(billing_client):
    values, previous, calls = receive_ambiguous(billing_client,last_update=True)
    _, app, extractor, _, _, telegram, chat = values
    decision = choice(telegram,'same')
    document_id = app.state.telegram.workflow.pending(owner(app))['document_id']
    response, _ = chat(data=decision)
    assert not response.json().get('rate_limited')
    current = app.state.telegram.workflow.active(owner(app))
    assert current['id']==previous['id'] and current['document_id']==document_id
    assert current['portal_source_pending'] and current['folio']==previous['folio']
    assert extractor.calls==calls and app.state.billing.store.count('document',owner(app))==3
    chat(data=decision)
    assert app.state.telegram.workflow.active(owner(app))['document_id']==document_id
    assert app.state.billing.store.count('ticket',owner(app))==1 and extractor.calls==calls


def test_other_choice_pauses_previous_and_opens_a_new_request_without_mixing_data(billing_client):
    values, previous, calls = receive_ambiguous(billing_client)
    _, app, extractor, _, _, telegram, chat = values
    decision = choice(telegram,'other')
    document_id = app.state.telegram.workflow.pending(owner(app))['document_id']
    chat(data=decision)
    current = app.state.telegram.workflow.active(owner(app))
    assert current['id']!=previous['id'] and current['document_id']==document_id
    assert current['status']=='TICKET_RECIBIDO' and current['profile_id']==previous['profile_id']
    assert all(current.get(key) is None for key in ('merchant','folio','total','purchase_date','store_number'))
    assert app.state.billing.resolve_ticket(owner(app),previous['id'])['telegram_workflow']=='paused'
    assert app.state.billing.resolve_ticket(owner(app),previous['id'])['document_id']==previous['document_id']
    assert extractor.calls==calls and app.state.billing.store.count('profile',owner(app))==1
    chat(data=decision)
    assert app.state.billing.store.count('ticket',owner(app))==2 and extractor.calls==calls
    assert 'ya fue utilizada' in telegram.messages[-1][1]


@pytest.mark.parametrize('kind',['receipt','portal'])
def test_requested_field_photo_keeps_its_recorded_expectation_and_applies_without_a_ticket_choice(billing_client, kind):
    if kind=='receipt':
        extractor = Modern(); extractor.found = None; extractor.ticket['total'] = None
        values, _ = modern(billing_client,extractor)
        _, app, _, _, _, telegram, chat = values
        chat(data=button(telegram,'an'))
        ticket = app.state.telegram.workflow.active(owner(app))
        chat(data='td:'+ticket['id']+':total')
        assert app.state.telegram._state(owner(app))['ticket_input']['field']=='total'
    else:
        class Crops(ScopedExtractor):
            async def extract_field(self,*args):return FieldExtraction(value='00042')

        values, browser, _ = prepared(billing_client,extractor=Crops())
        _, app, _, _, _, telegram, chat = values
        enable_transactions(browser,PortalPages())
        ticket = app.state.telegram.workflow.active(owner(app))
        app.state.billing.store.update('ticket',owner(app),ticket['id'],ticket|{'store_number':None})
        chat(data='lv:'+ticket['id'])
        state_now = app.state.telegram._state(owner(app))
        assert 'portal_field_input' in state_now, 'DIAGNOSTICO ultimo mensaje: '+repr(telegram.messages[-1][1])[:600]+' botones: '+repr(telegram.messages[-1][2])[:400]
        expectation = state_now['portal_field_input']
        assert expectation['ticket_id']==ticket['id'] and expectation['transaction_id']
    telegram.files['field'] = image('yellow'); chat(document='field')
    assert app.state.telegram.workflow.pending(owner(app)) is None
    assert app.state.telegram.workflow.active(owner(app))['document_id']==ticket['document_id']
    assert app.state.billing.store.count('portal_field_photo',owner(app))==1
    fill_daily_quota(app)
    chat(data=button(telegram,'la')); chat(data=button(telegram,'lu'))
    if kind=='receipt':
        assert app.state.billing.resolve_ticket(owner(app),ticket['id'])['total']=='175.50'
    else:
        transaction = app.state.billing.portal_submission.current(owner(app),ticket['id'])
        assert list(transaction['manual'].values())==['00042']
    assert app.state.billing.store.count('ticket',owner(app))==1


@pytest.mark.parametrize('invalid',['expired','foreign','superseded'])
def test_expired_foreign_or_superseded_decision_never_changes_the_active_source(billing_client, invalid):
    values, previous, calls = receive_ambiguous(billing_client)
    _, app, extractor, _, _, telegram, chat = values
    decision = choice(telegram,'same')
    if invalid=='expired':
        with app.state.database.connection() as conn:
            conn.execute('UPDATE billing_records SET expires=? WHERE id=?',(time.time()-1,decision.split(':')[1]))
    elif invalid=='superseded':
        chat('/cancelar')
    chat(data=decision,user=456 if invalid=='foreign' else 123)
    assert app.state.telegram.workflow.active(owner(app))['document_id']==previous['document_id']
    assert app.state.billing.store.count('ticket',owner(app))==1 and extractor.calls==calls
    assert 'venció' in telegram.messages[-1][1] or 'menú cambió' in telegram.messages[-1][1]


@pytest.mark.parametrize('action',['same','other'])
def test_pending_photo_counts_once_at_document_limit_and_decisions_reuse_it(billing_client, action):
    values, previous, calls = receive_ambiguous(billing_client,maximum=3)
    _, app, extractor, _, _, telegram, chat = values
    assert app.state.billing.store.count('document',owner(app))==3
    chat(data=choice(telegram,action))
    current = app.state.telegram.workflow.active(owner(app))
    assert app.state.billing.store.count('document',owner(app))==3 and extractor.calls==calls
    telegram.files['overflow'] = image('red'); chat(document='overflow')
    assert 'Archivos guardados: 3/3' in telegram.messages[-1][1]
    assert app.state.billing.store.count('document',owner(app))==3 and extractor.calls==calls
    assert app.state.telegram.workflow.active(owner(app))['document_id']==current['document_id']


def test_identical_bytes_keep_the_same_ticket_without_an_ambiguity_prompt_or_new_read(billing_client):
    extractor = Modern(); extractor.found = None; extractor.ticket['total'] = None
    values, _ = modern(billing_client,extractor)
    _, app, extractor, _, _, telegram, chat = values
    chat(data=button(telegram,'an'))
    previous = app.state.telegram.workflow.active(owner(app)); calls = list(extractor.calls)
    chat(document='receipt')
    assert app.state.telegram.workflow.active(owner(app))['document_id']==previous['document_id']
    assert app.state.telegram.workflow.pending(owner(app)) is None
    assert app.state.billing.store.count('document',owner(app))==2 and extractor.calls==calls
