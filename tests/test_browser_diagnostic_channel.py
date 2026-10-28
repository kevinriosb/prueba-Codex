import httpx2
import pytest

from backend.app.billing.browser import PortalBrowserClient
from backend.app.config import Settings
from backend.app.errors import AppError
from .test_billing import billing_client
from .test_portal_budgets import received,ENTRY
from .test_telegram_agent import owner,button
from .test_adaptive_browser import snapshot


@pytest.mark.parametrize('reason',['size_limit','page_size_limit'])
def test_size_exhaustion_can_resume_same_ticket_from_saved_public_address(billing_client,reason):
    values,browser,model=received(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    user=owner(app);billing=app.state.billing
    ticket=billing.store.list('ticket',user)[0]
    review=billing.browser_review(user,ticket['id'])
    review['snapshot']['block_reason']=reason
    billing.store.update('browser_review',user,review['id'],review)
    client.portal.call(app.state.telegram._browser,user,123,ticket['id'])
    assert any(label=='Retomar desde esta página' for label,_ in telegram.messages[-1][2])
    chat(data=button(telegram,'bn'))
    assert [item[0] for item in browser.calls]==['open','close','open']
    assert browser.calls[-1][2]==ENTRY and len(extractor.calls)==2
    assert mail.sent==sat.calls==[]


def test_native_diagnostic_is_owned_read_only_and_works_at_ai_and_update_limit(billing_client):
    values,browser,model=received(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    user=owner(app);billing=app.state.billing
    ticket=billing.store.list('ticket',user)[0]
    review=billing.browser_review(user,ticket['id'])
    review['snapshot']['diagnostics']={'requests':30,'page_requests':12,'transferred_bytes':7_000_000,
        'page_bytes':1_000_000,'cache_hits':5,'cache_bytes':500_000,
        'issues':[{'kind':'script','site':'https://provider.example','code':'response_size_limit'}]}
    billing.store.update('browser_review',user,review['id'],review)
    client.portal.call(app.state.telegram._browser,user,123,ticket['id'])
    action=button(telegram,'bd')
    before=list(browser.calls);inputs=len(model.inputs)
    app.state.settings.max_daily_runs=1
    app.state.settings.billing_max_daily_telegram_updates=1
    chat(data=action,user=456)
    assert 'no encontrado' in telegram.messages[-1][1]
    chat(data=action)
    assert 'Diagnóstico guardado' in telegram.messages[-1][1]
    assert 'archivo demasiado grande' in telegram.messages[-1][1]
    assert browser.calls==before and len(model.inputs)==inputs
    assert app.state.database.usage(user)['runs']==1


async def test_protocol_mismatch_has_actionable_error_without_raw_response(tmp_path):
    token=tmp_path/'token';token.write_text('fake-worker-token-'+'x'*40)
    settings=Settings(_env_file=None,app_login_password='test-only-password-123',
        billing_browser_enabled=True,billing_browser_token_file=token)
    def transport(_):return httpx2.Response(200,json={'unexpected':'never-print-secret-payload'})
    client=PortalBrowserClient(settings,client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)))
    try:
        with pytest.raises(AppError) as error:
            await client.open('owner','https://merchant.example/',allowed=['https://merchant.example'])
        assert error.value.code=='browser_protocol' and 'never-print' not in error.value.message
    finally:await client.close()


def test_new_job_resumes_when_live_budget_exhausted_after_saved_observation(billing_client):
    values,browser,model=received(billing_client)
    client,app,extractor,mail,sat,telegram,chat=values
    billing=app.state.billing;user=owner(app)
    ticket=billing.store.list('ticket',user)[0]
    record=billing.browser_review(user,ticket['id'])
    record['snapshot'].update(network_blocked=False,block_reason=None)
    billing.store.update('browser_review',user,record['id'],record)
    # La observación viva del navegador ya informa de presupuesto agotado.
    chat(data=button(telegram,'bn'))
    assert [item[0] for item in browser.calls]==['open','observe','close','open']
    assert browser.calls[-1][2]==ENTRY and app.state.database.usage(user)['runs']==2


@pytest.mark.parametrize('foreign_session',[False,True])
async def test_worker_boundary_and_wrong_session_do_not_get_reported_as_safe_success(tmp_path,foreign_session):
    token=tmp_path/'token';token.write_text('fake-worker-token-'+'x'*40)
    settings=Settings(_env_file=None,app_login_password='test-only-password-123',
        billing_browser_enabled=True,billing_browser_token_file=token)
    result=snapshot()
    if not foreign_session:result['submitted']=True
    def transport(_):return httpx2.Response(200,json=result)
    client=PortalBrowserClient(settings,client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)))
    try:
        with pytest.raises(AppError) as error:
            await client.observe('owner','22222222-2222-4222-8222-222222222222' if foreign_session else result['session_id'])
        assert error.value.code==('browser_protocol' if foreign_session else 'browser_boundary')
    finally:await client.close()
