"""Interpretar etiquetas observadas no rellena controles ni permite cambiar su identidad."""
from ast import literal_eval

import pytest

from backend.app.billing.browser import FieldMeaning
from backend.app.errors import AppError
from .billing_fakes import image
from .fake_model import ScriptedModel,call
from .test_billing import billing_client
from .test_portal_preparation import RealBrowser,ScopedExtractor,ROOT
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile


class MeaningModel(ScriptedModel):
    async def get_response(self,system_instructions,input,*args,**kwargs):
        if not self.inputs:self.outputs=[[call('portal_browser_observe')]]
        else:
            metadata=literal_eval(next(item['output'] for item in reversed(input) if item.get('type')=='function_call_output'))
            assert all(field['key'] is None for field in metadata['fields'])
            self.outputs=[[call('portal_browser_observe',observation_id=metadata['observation_id'],
                field_meanings=[{'reference':field['reference'],'key':key} for field,key in zip(metadata['fields'],['rfc','folio'])])]]
        return await super().get_response(system_instructions,input,*args,**kwargs)


def test_unknown_field_labels_can_be_interpreted_from_current_observation_without_fixed_keywords(billing_client):
    browser=RealBrowser();browser.pages.pages={ROOT:'<h1>Steuerbeleg</h1><form><label>Steuerkennzeichen<input required></label><label>Kassenbonnummer<input required></label></form>'}
    model=MeaningModel()
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser,extractor=ScopedExtractor())
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'));chat(data=button(telegram,'wa'))
    item=app.state.billing.store.list('ticket',owner(app))[0]
    observed=app.state.billing.browser_review(owner(app),item['id'])['snapshot']
    assert 'Formulario encontrado' in telegram.messages[-1][1]
    assert [field['key'] for field in observed['fields']]==['rfc','folio']
    assert len(model.inputs)==2 and 'fill' not in browser.calls and mail.sent==sat.calls==[]
    session=browser.manager.sessions[observed['session_id']]
    assert client.portal.call(session.page.locator('input').evaluate_all,'nodes=>nodes.map(e=>e.value)')==['','']


@pytest.mark.parametrize('change',['stale','foreign','recognized','repeated'])
def test_interpretation_rejects_stale_foreign_or_reinterpreted_field_references(billing_client,change):
    from .test_adaptive_browser import FakeBrowser,grant
    from .test_billing import ticket
    browser=FakeBrowser();client,app,extractor,*_=billing_client(browser=browser)
    extractor.ticket['billing_url']=ROOT;item=ticket(client);grant(client,item['id'])
    record=client.portal.call(app.state.billing.browse_portal,'owner',item['id'])
    record['snapshot']['fields']=[{'reference':'1234abcd','key':'rfc','label':'RFC','kind':'input','required':True,'options':[]}]
    record=app.state.billing.store.update('browser_review','owner',record['id'],record)
    user='foreign' if change=='foreign' else 'owner'
    observation='deadbeef' if change=='stale' else record['snapshot']['observation_id']
    key='folio' if change=='recognized' else 'rfc'
    meanings=[FieldMeaning(reference='1234abcd',key=key)]
    if change=='repeated':meanings.append(meanings[0])
    with pytest.raises(AppError):
        app.state.billing.portal_preparation.describe_fields(user,item['id'],observation,meanings)
    saved=app.state.billing.browser_review('owner',item['id'])
    assert saved['snapshot']['fields'][0]['key']=='rfc' and browser.calls[0][0]=='open'
