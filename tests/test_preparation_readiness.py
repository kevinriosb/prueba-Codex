"""El plazo de ruta puede pausarla: la fixture de datos debe completar su precondición."""
from types import SimpleNamespace

import pytest

from backend.app.billing import tools
from .test_billing import billing_client
from .test_portal_preparation import prepared
from .test_telegram_agent import owner


@pytest.mark.parametrize('require_ready',[False,True])
def test_slow_open_can_exhaust_route_before_form_without_a_background_navigation(billing_client,monkeypatch,require_ready):
    clock=[100.0]
    monkeypatch.setattr(tools,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    def slow_open(app,ticket):
        browser=app.state.billing.browser
        original=browser.open
        async def opening(*args,**kwargs):
            result=await original(*args,**kwargs)
            clock[0]+=23  # Solo el reloj de la ruta; no duerme ni altera asyncio.
            return result
        browser.open=opening
    values,browser,_=prepared(billing_client,before_portal=slow_open,require_ready=require_ready)
    _,app,extractor,*_=values
    ticket=app.state.telegram.workflow.active(owner(app))
    if require_ready:
        assert ticket['portal_stage']=='data_read' and browser.calls==['open','act']
        assert all(ticket.get(key) for key in ('total','folio','store_number','purchase_date'))
        return
    assert browser.calls==['open']
    assert ticket['portal_stage']=='awaiting_form'
    assert all(ticket.get(key) is None for key in ('total','folio','store_number','purchase_date'))
    assert not ticket.get('portal_browser_error')
    assert [item[0] for item in extractor.calls]==['csf','routing']
    review=app.state.billing.browser_review(owner(app),ticket['id'])
    assert not review['snapshot']['fields'] and review['snapshot']['actions']
