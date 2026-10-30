"""Chromium y SDK reales, modelo y red simulados. Ningún sitio externo."""
from ast import literal_eval
import json

import pytest

from backend.app.agent_service import BROWSER_INSTRUCTIONS
from .billing_fakes import FISCAL,image
from .fake_model import ScriptedModel,call,answer
from .test_billing import billing_client
from .test_browser_worker import Pages
from .test_portal_preparation import RealBrowser,ScopedExtractor,ROOT
from .test_telegram_agent import create,button,owner
from .test_telegram_documents import confirm_profile


FORM='<form method="post" action="/never-send"><label>RFC<input required></label><label>Web ID<input required></label></form>'
# Las claves solo guían al DOBLE del modelo, nunca al código de producción.
LAYOUTS={
    'footer_fr':({'/':'<main>Bienvenue</main><footer><a href="/end">Justificatif fiscal</a></footer>','/end':FORM},['Justificatif']),
    'dropdown':({'/':'''<nav><button type=button onclick="document.getElementById('links').hidden=false">Opciones</button>
        <div id=links hidden><a href="/end">Documentos de consumo</a></div></nav>''','/end':FORM},['Opciones','Documentos de consumo']),
    'icon_context':({'/':'''<section><h2>Obtén el comprobante de tu consumo</h2><button type=button onclick="document.querySelector('#panel').hidden=false">
        <svg viewBox="0 0 20 20"><title>Documento tributario</title><path d="M2 2h10v15H2z"/></svg></button></section><div id=panel hidden>'''+FORM+'</div>'},['Documento tributario']),
    'picture_card':({'/':'<article><h2>Área de documentos</h2><a href="/end"><img src="/opaque-4.png" alt="Comprobantes de consumo"></a></article>','/end':FORM},['Comprobantes de consumo']),
    'tab':({'/':'''<div role=tablist><button role=tab aria-selected=false onclick="document.getElementById('panel').hidden=false;this.setAttribute('aria-selected','true')">Mis comprobantes</button></div><div id=panel hidden>'''+FORM+'</div>'},['Mis comprobantes']),
    'german_intermediate':({'/':'<a href="/middle">Steuerbeleg</a>','/middle':'<p>Ihr Verbrauchsdokument</p><a href="/end">Weiter zum Beleg</a>','/end':FORM},['Steuerbeleg','Weiter zum Beleg']),
    'select':({'/':'''<label>Elige unidad<select onchange="location.href=this.value"><option value="">Elige</option><option value="/end">Cocina Aurora</option><option value="/else">Otra cocina</option></select></label>''','/end':FORM},['Cocina Aurora']),
    'shadow_menu':({'/':'''<div id=host></div><script>document.getElementById('host').attachShadow({mode:'open'}).innerHTML='<a href="/end">Receipt desk</a>'</script>''','/end':FORM},['Receipt desk']),
}


class IntentModel(ScriptedModel):
    def __init__(self,choices):
        super().__init__();self.choices=list(choices)
    async def get_response(self,system_instructions,input,*args,**kwargs):
        assert BROWSER_INSTRUCTIONS in system_instructions
        if not self.inputs:
            self.outputs=[[call('portal_browser_observe')]]
        else:
            metadata=literal_eval(next(item['output'] for item in reversed(input) if item.get('type')=='function_call_output'))
            assert metadata['public_only'] and not metadata['submitted']
            hint=self.choices.pop(0)
            action=next((item for item in metadata['actions'] if hint in item['label']),None)
            if action is None:action=next(item for item in metadata['actions'] if hint in item.get('context',''))
            self.outputs=[[call('portal_browser_navigate',observation_id=metadata['observation_id'],reference=action['reference'])]]
        return await super().get_response(system_instructions,input,*args,**kwargs)


@pytest.mark.parametrize('layout',list(LAYOUTS))
def test_agent_can_follow_unfamiliar_page_controls_by_observation_within_existing_limits(billing_client,layout):
    markup,choices=LAYOUTS[layout]
    browser=RealBrowser();browser.pages.pages={ROOT.rstrip('/')+path:html for path,html in markup.items()}
    model=IntentModel(choices)
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser,extractor=ScopedExtractor())
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'));chat(data=button(telegram,'wa'))
    result=app.state.billing.store.list('ticket',owner(app))[0]
    record=app.state.billing.browser_review(owner(app),result['id'])
    assert 'Formulario encontrado' in telegram.messages[-1][1],telegram.messages[-1][1]
    assert {item['key'] for item in record['snapshot']['fields']}=={'rfc','web_id'}
    assert len(model.inputs)<=app.state.settings.max_agent_turns
    assert browser.manager.sessions[record['snapshot']['session_id']].steps<=8
    assert 'fill' not in browser.calls and mail.sent==sat.calls==[]
    assert all(secret not in json.dumps(model.inputs) for secret in (FISCAL['rfc'],FISCAL['name']))
    values=client.portal.call(browser.manager.sessions[record['snapshot']['session_id']].page.locator('input').evaluate_all,'nodes=>nodes.map(e=>e.value)')
    assert values==['','']


def test_dead_end_explains_current_location_and_offers_manual_address_without_retry(billing_client):
    browser=RealBrowser();browser.pages.pages={ROOT:'<h1>Información corporativa</h1><p>No hay accesos disponibles.</p>'}
    model=ScriptedModel([[call('portal_browser_observe')],[answer('No hay un acceso público visible.')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser,extractor=ScopedExtractor())
    confirm_profile(telegram,chat)
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'));chat(data=button(telegram,'wa'))
    assert browser.calls==['open'] and len(model.inputs)==2
    assert 'Aún no se encontró el formulario' in telegram.messages[-1][1]
    assert any(data.startswith('dw:') for _,data in telegram.messages[-1][2])
    assert mail.sent==sat.calls==[]
