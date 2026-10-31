"""Resolver revisiones preparadas usa controles; crear trabajo conserva su cuota."""
import time
import uuid
from datetime import datetime, timezone

import pytest

from .billing_fakes import image, pdf, xml
from .fake_model import ScriptedModel, call
from .test_billing import billing_client
from .test_limits import write_spec
from .test_portal_preparation import prepared
from .test_portal_submission import PortalPages, enable_transactions
from .test_telegram_agent import button, create, owner
from .test_telegram_workflow import first_ticket


def fill_daily_quota(app, *, user=123, kind='channel_update', maximum=100):
    account = owner(app,user)
    store = app.state.billing.store
    today = datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    with store.database.connection() as conn:
        used = conn.execute('SELECT COUNT(*) FROM billing_records WHERE kind=? AND owner=? AND created>=?',
            (kind,account,today)).fetchone()[0]
    assert used <= maximum
    for _ in range(maximum-used):
        store.add(kind,account,{},lookup=store.digest('quota-test:'+uuid.uuid4().hex),expires=time.time()+86400)


def prepared_portal(factory):
    values, browser, _ = prepared(factory)
    client, app, extractor, mail, sat, telegram, chat = values
    pages = PortalPages(); pages.invoice_xml = xml(); pages.invoice_pdf = pdf()
    enable_transactions(browser,pages)
    ticket = app.state.billing.store.list('ticket',owner(app))[0]
    chat(data='lv:'+ticket['id'])
    chat(data=button(telegram,'ls'))
    assert pages.sent==[]
    return values, pages, ticket['id'], button(telegram,'lc')


def test_prepared_portal_approval_works_at_message_limit_and_never_sends_twice(billing_client):
    values, pages, _, approval = prepared_portal(billing_client)
    client, app, extractor, mail, sat, telegram, chat = values
    fill_daily_quota(app)
    controls = app.state.billing.store.count('channel_control',owner(app))
    response, payload = chat(data=approval)
    assert not response.json().get('rate_limited')
    assert len([request for request in pages.sent if request[0]=='POST']) == 1
    assert app.state.telegram.requests.usage(owner(app))['updates'] == 100
    assert app.state.billing.store.count('channel_control',owner(app)) == controls+1
    sent = len(pages.sent)
    duplicate = client.post('/integrations/telegram/webhook',json=payload,
        headers={'X-Telegram-Bot-Api-Secret-Token':'x'*40})
    assert duplicate.json()['duplicate'] and len(pages.sent)==sent
    chat(data=approval)  # Otro update_id después de consumir la autorización.
    assert len(pages.sent)==sent
    model_calls = len(app.state.service.agent.model.inputs)
    response, _ = chat('Una nueva consulta de texto libre')
    assert response.json()['rate_limited']
    telegram.files['new-photo'] = image('yellow')
    downloads = list(telegram.downloads)
    response, _ = chat(document='new-photo')
    assert response.json()['rate_limited'] and telegram.downloads==downloads
    assert len(app.state.service.agent.model.inputs)==model_calls and mail.sent==[]


@pytest.mark.parametrize('decision',['se','no'])
def test_pending_email_can_be_approved_or_rejected_at_message_limit(billing_client, decision):
    values, ticket_id = first_ticket(billing_client,billing_live_email_enabled=True)
    client, app, extractor, mail, sat, telegram, chat = values
    chat(data='ve:'+ticket_id); chat(data='pr:'+ticket_id)
    approval_id = button(telegram,'se').split(':')[1]
    fill_daily_quota(app)
    response, _ = chat(data=decision+':'+approval_id)
    assert not response.json().get('rate_limited')
    assert len(mail.sent)==(1 if decision=='se' else 0)
    record = app.state.billing.store.get('email_approval',owner(app),approval_id)
    assert record['_state']!= 'pending'
    if decision=='no':assert record['_state']=='rejected'
    chat(data=decision+':'+approval_id)
    assert len(mail.sent)==(1 if decision=='se' else 0)


def test_sat_confirmation_for_a_validated_invoice_works_at_message_limit(billing_client):
    values, pages, ticket_id, approval = prepared_portal(billing_client)
    client, app, extractor, mail, sat, telegram, chat = values
    chat(data=approval)
    chat(data='sa:'+ticket_id)
    confirmation = button(telegram,'sv')
    assert sat.calls==[]
    fill_daily_quota(app)
    response, _ = chat(data=confirmation)
    assert not response.json().get('rate_limited') and len(sat.calls)==1
    assert app.state.billing.resolve_ticket(owner(app),ticket_id)['validation']['sat_verified']


@pytest.mark.parametrize('decision',['ga','gr'])
def test_sdk_pending_approval_or_rejection_uses_control_quota(billing_client, decision):
    writes = []
    model = ScriptedModel([[call('external_change',note='Cambio aprobado')]])
    client, app, _, _, _, telegram, chat = create(billing_client,model,extras=(write_spec(writes),))
    chat('Prepara un cambio')
    approval = button(telegram,decision)
    fill_daily_quota(app)
    response, _ = chat(data=approval)
    assert not response.json().get('rate_limited')
    assert writes==(['Cambio aprobado'] if decision=='ga' else [])
    chat(data=approval)
    assert writes==(['Cambio aprobado'] if decision=='ga' else [])


@pytest.mark.parametrize('decision',['pf','pd'])
def test_prepared_fiscal_review_can_be_confirmed_or_discarded_at_message_limit(billing_client, decision):
    client, app, _, _, _, telegram, chat = create(billing_client)
    telegram.files['csf'] = image('blue')
    chat(document='csf',caption='/csf'); chat(data=button(telegram,'an'))
    chat('G03'); chat('cliente@example.com')
    approval = button(telegram,decision)
    fill_daily_quota(app)
    response, _ = chat(data=approval)
    assert not response.json().get('rate_limited')
    assert app.state.billing.store.count('profile',owner(app))==(1 if decision=='pf' else 0)
    assert app.state.billing.store.count('profile_draft',owner(app))==0


@pytest.mark.parametrize('invalid',['expired','consumed','foreign'])
def test_only_an_owned_pending_review_can_use_control_quota(billing_client, invalid):
    values, pages, _, approval = prepared_portal(billing_client)
    client, app, extractor, mail, sat, telegram, chat = values
    identifier = approval.split(':')[1]
    user = 456 if invalid=='foreign' else 123
    if invalid=='expired':
        with app.state.database.connection() as conn:
            conn.execute('UPDATE billing_records SET expires=? WHERE id=?',(time.time()-1,identifier))
    elif invalid=='consumed':
        app.state.billing.store.claim('portal_send_review',owner(app),identifier)
    fill_daily_quota(app,user=user)
    response, _ = chat(data=approval,user=user)
    assert response.json()['rate_limited'] and pages.sent==[] and mail.sent==[]


def test_prepared_approval_still_respects_the_separate_500_control_limit(billing_client):
    values, pages, _, approval = prepared_portal(billing_client)
    _, app, _, _, _, _, chat = values
    fill_daily_quota(app)
    fill_daily_quota(app,kind='channel_control',maximum=500)
    response, _ = chat(data=approval)
    assert response.json()['rate_limited'] and pages.sent==[]
    assert app.state.billing.store.get('portal_send_review',owner(app),approval.split(':')[1])['_state']=='pending'
