"""Orden real de autorización con SDK/Chromium reales y datos inventados."""
import json
import logging

import pytest

from backend.app.billing.models import TicketExtraction
from .billing_fakes import image
from .fake_model import ScriptedModel, call
from .test_billing import billing_client
from .test_portal_preparation import RealBrowser, ScopedExtractor, ROOT
from .test_telegram_agent import create, button, owner
from .test_telegram_documents import confirm_profile


class Printed(ScopedExtractor):
    routing_version=3

    async def extract_routing(self,mime,data):
        values={key:None for key in TicketExtraction.model_fields}
        values.update(is_ticket=True,readable=True,email_explicitly_for_billing=False,merchant='Comercio inventado',
            billing_url=ROOT,printed_addresses=[{'url':ROOT,'purpose':'billing',
                'evidence':'Obtén tu factura en '+ROOT}])
        return TicketExtraction.model_validate(values)


def portal_events(caplog):
    return [json.loads(record.message) for record in caplog.records if record.name=='personal_agent.portal']


@pytest.mark.parametrize('meanings',[[],[{'reference':'1234abcd','key':'rfc'}]])
def test_first_observe_with_field_meanings_opens_before_interpreting(billing_client,caplog,meanings):
    caplog.set_level(logging.INFO,logger='personal_agent.portal')
    browser=RealBrowser();browser.pages.pages={ROOT:'<form><label>RFC<input required></label><label>Ticket<input required></label></form>'}
    model=ScriptedModel([[call('portal_browser_observe',field_meanings=meanings)]])
    probed=[]
    async def probe(url):
        ticket=app.state.billing.store.list('ticket',owner(app))[0]
        assert ticket['billing_url'] is None and ticket['portal_stage']=='awaiting_entry'
        assert ticket['portal_printed_url_candidate']==ROOT
        assert not app.state.billing.portal_permissions.allowed(owner(app),ticket)
        assert not app.state.billing.store.list('browser_review',owner(app))
        probed.append(url)
        return {'url':url,'reachable':True,'reason':None,'status':200}
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser,
        extractor=Printed(),portal_probe=probe)
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('red')
    chat(document='receipt')
    caplog.clear()
    chat(data=button(telegram,'an'))
    ticket=app.state.telegram.workflow.active(owner(app))
    events=portal_events(caplog)
    # La hipótesis original deja aquí browser_session / describe_fields, sin open.
    assert browser.calls==['open'], events
    assert probed==[ROOT] and ticket['portal_stage']=='data_read'
    assert not ticket.get('portal_browser_error')
    assert 'Formulario encontrado' in telegram.messages[-1][1]
    assert any(e['phase']=='opening' and e['permission_current'] and not e['browser_review_exists'] for e in events)
    assert all(not e['scope_changed'] for e in events)
    assert not any(e['code']=='browser_session' and e['tool']=='portal_browser_observe' for e in events)
    assert mail.sent==sat.calls==[] and 'fill' not in browser.calls
    assert len(model.inputs)==1
    from backend.app.billing.portal_diagnostics import STEP_FIELDS,validate_step
    base={'event','tool','phase','code','scope_changed','browser_review_exists','permission_current'}
    assert all(set(e)==base or set(e)==base|STEP_FIELDS for e in events)
    for e in events:
        if 'step' in e:validate_step({k:e[k] for k in STEP_FIELDS})


def test_double_analysis_click_does_not_start_a_second_review(billing_client):
    import asyncio

    class GatedModel(ScriptedModel):
        async def get_response(self,*args,**kwargs):
            self.started.set()
            await self.release.wait()
            return await super().get_response(*args,**kwargs)

    model=GatedModel([[call('portal_browser_observe',field_meanings=[])]])
    browser=RealBrowser();browser.pages.pages={ROOT:'<label>RFC<input></label>'}
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser,extractor=Printed())
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('red');chat(document='receipt')
    data=button(telegram,'an')
    message={'chat':{'id':123,'type':'private'}}
    def payload(identifier):
        return {'update_id':identifier,'callback_query':{'from':{'id':123},'message':message,'data':data}}

    async def race():
        model.started=asyncio.Event();model.release=asyncio.Event()
        first=asyncio.create_task(app.state.telegram.handle(payload(5001)))
        try:
            await asyncio.wait_for(model.started.wait(),timeout=5)
            second=await app.state.telegram.handle(payload(5002))
            assert second.get('busy') and 'Ya estoy revisando' in telegram.messages[-1][1]
        finally:
            model.release.set()
            await first

    client.portal.call(race)
    assert len(model.inputs)==1 and browser.calls==['open']
    assert app.state.database.usage(owner(app))['runs']==1
    assert not app.state.telegram._inflight_callbacks
    assert mail.sent==sat.calls==[]


def test_changed_scope_never_reuses_grant_or_checkpoint_and_logs_no_private_data(billing_client,caplog):
    from agents.tool_context import ToolContext
    from backend.app.billing.tools import build_billing_tools
    from .test_portal_preparation import prepared

    values,browser,_=prepared(billing_client,require_ready=True)
    client,app,extractor,mail,sat,telegram,chat=values
    billing=app.state.billing;user=owner(app)
    ticket=app.state.telegram.workflow.active(user)
    before=list(browser.calls)
    grants=[item['id'] for item in billing.store.list('portal_grant',user)]
    ticket['billing_url']='https://another.example/form?token=PRIVATE-DIAGNOSTIC-SECRET'
    billing.store.update('ticket',user,ticket['id'],ticket)
    assert not billing.portal_permissions.allowed(user,ticket)
    assert billing.portal_resume_url(user,ticket) is None
    tool=next(t for t in build_billing_tools(billing) if t.name=='portal_browser_observe')
    context=ToolContext(tool_name=tool.name,tool_call_id='scope-test',tool_arguments='{}',context={'user_id':user,'purpose':'portal_review','channel':'telegram',
        'channel_context':{'browser_ticket_id':ticket['id']},'events':[]})
    caplog.set_level(logging.INFO,logger='personal_agent.portal');caplog.clear()
    client.portal.call(tool.on_invoke_tool,context,json.dumps({'observation_id':'1234abcd',
        'field_meanings':[{'reference':'1234abcd','key':'rfc'}]}))
    assert browser.calls==before
    assert [item['id'] for item in billing.store.list('portal_grant',user)]==grants
    assert any(e['review']['kind']=='portal_permission' for e in context.context['events'])
    events=portal_events(caplog)
    assert any(e['scope_changed'] and not e['permission_current'] for e in events)
    assert 'PRIVATE-DIAGNOSTIC-SECRET' not in caplog.text and 'another.example' not in caplog.text
    assert mail.sent==sat.calls==[]
