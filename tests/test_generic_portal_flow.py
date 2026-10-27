"""Recorridos distintos con el SDK real y páginas/modelo simulados, sin APIs de pago."""
import json
from ast import literal_eval

import pytest

from backend.app.agent_service import BROWSER_INSTRUCTIONS
from .billing_fakes import FISCAL,image
from .fake_model import ScriptedModel,answer,call
from .test_adaptive_browser import FakeBrowser,grant,snapshot
from .test_billing import billing_client,ticket
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile


class InstructionsModel(ScriptedModel):
    async def get_response(self,system_instructions,*args,**kwargs):
        self.instructions=system_instructions
        return await super().get_response(system_instructions,*args,**kwargs)


@pytest.mark.parametrize('logos,field',[(0,'folio'),(1,'account_number'),(2,'reference_number'),(3,'billing_code')])
def test_non_brand_portals_continue_from_current_controls_without_forcing_a_brand(billing_client,logos,field):
    class DifferentLayout(FakeBrowser):
        def page(self,observed='aaaaaaaa',reference='bbbbbbbb'):
            result=snapshot(actions=[{'reference':reference,'label':'Acceso a facturación','kind':'link'}]+
                [{'reference':str(index+1)*8,'label':(['Comercio Independiente','Proveedor de pagos','Otro acceso'][index]
                    if logos==3 else 'Comercio Independiente'),
                  'kind':'link','brand':True} for index in range(logos)],observed=observed)
            return result
        async def open(self,owner,url,*,allowed=None):
            self.calls.append(('open',owner,url))
            return self.page()
        async def observe(self,owner,session):
            self.calls.append(('observe',owner,session))
            return self.page('dddddddd','eeeeeeee')
        async def act(self,owner,session,observed,reference):
            assert (observed,reference)==('dddddddd','eeeeeeee')
            self.calls.append(('act',owner,session,observed,reference))
            return snapshot(fields=[{'key':field,'label':'Dato solicitado por este sitio','required':True,
                'kind':'input','options':[]}],observed='ffffffff')

    browser=DifferentLayout()
    model=InstructionsModel([[call('portal_browser_observe')],[answer('Acceso observado.')],
        [call('portal_browser_observe')],
        [call('portal_browser_navigate',observation_id='dddddddd',reference='eeeeeeee')],
        [answer('Formulario observado.')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser)
    confirm_profile(telegram,chat)
    extractor.ticket.update(merchant='Comercio Independiente',billing_url='https://merchant.example/')
    telegram.files['receipt']=image('red')
    chat(document='receipt');chat(data=button(telegram,'an'));chat(data=button(telegram,'wa'))
    if logos<3:
        assert not any(data.startswith('ba:') for _,data in telegram.messages[-1][2])
    assert any(label=='Continuar revisión con IA' for label,_ in telegram.messages[-1][2])
    chat(data=button(telegram,'bn'))
    assert [item[0] for item in browser.calls]==['open','observe','act']
    assert 'Dato solicitado por este sitio' in telegram.messages[-1][1]
    assert BROWSER_INSTRUCTIONS in model.instructions
    assert all(value not in json.dumps(model.inputs) for value in (FISCAL['rfc'],FISCAL['name']))
    assert len(extractor.calls)==2 and mail.sent==sat.calls==[]
    assert len(app.state.billing.store.list('profile',owner(app)))==1


def test_web_sdk_receives_same_generic_policy_and_can_inspect_a_direct_form(billing_client):
    class DirectForm(FakeBrowser):
        async def open(self,owner,url,*,allowed=None):
            self.calls.append(('open',owner,url))
            return snapshot(fields=[{'key':'reference_number','label':'Referencia de compra',
                'required':True,'kind':'input','options':[]}])
    browser=DirectForm()
    model=InstructionsModel()
    client,app,extractor,mail,sat,_=billing_client(model=model,browser=browser)
    extractor.ticket['billing_url']='https://merchant.example/'
    item=ticket(client)
    grant(client,item['id'])
    model.outputs=[[call('portal_browser_observe',identifier=item['id'])],[answer('Referencia visible.')]]
    conversation=client.post('/api/conversations').json()['id']
    response=client.post('/api/conversations/'+conversation+'/messages',json={'text':'Revisa el acceso de facturación.'})
    assert response.status_code==200,response.text
    assert BROWSER_INSTRUCTIONS in model.instructions
    assert [item[0] for item in browser.calls]==['open']
    metadata=next(literal_eval(item['output']) for item in model.inputs[-1] if item.get('type')=='function_call_output')
    assert metadata['fields'][0]['key']=='reference_number'
    assert metadata['actions']==[] and metadata['submitted'] is False
    assert mail.sent==sat.calls==[]
