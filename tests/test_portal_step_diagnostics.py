"""Solo tres escenarios locales: asistente, formulario inicial y verificación humana."""
import json
import logging
from ast import literal_eval
from functools import partial

import pytest

from backend.app.billing.portal_diagnostics import STEP_FIELDS,step_event,validate_step
from backend.app.errors import AppError
from .billing_fakes import FISCAL,TICKET
from .fake_model import ScriptedModel,call,answer
from .test_billing import billing_client
from .test_portal_preparation import prepared,ROOT
from .test_telegram_agent import owner

FORM='<form action="/never-send" method="post"><label>RFC<input required></label><label>Ticket<input required></label></form>'
PAGES={
    'wizard':{ROOT:'''<h1>Encuesta de satisfacción</h1><button type=button onclick="location.href='/access'">Continuar sin evaluar</button>''',
        ROOT+'access':'''<form><label>Contraseña<input type=password></label><button>Iniciar sesión</button></form>
        <button type=button onclick="location.href='/form'">Continuar sin cuenta</button>''',ROOT+'form':FORM},
    'initial_form':{ROOT:FORM},
    'captcha':{ROOT:FORM+'<div class=g-recaptcha>Verificación humana</div>'},
    'unfillable':{ROOT:FORM+'<label>Adjunta imagen de verificación<input type=file required></label>'},
}


class ObservingModel(ScriptedModel):
    """Escoge referencias observadas; no compara texto, marcas ni dominios."""
    async def get_response(self,system_instructions,input,*args,**kwargs):
        if not self.inputs:self.outputs=[[call('portal_browser_observe')]]
        else:
            metadata=literal_eval(next(i['output'] for i in reversed(input) if i.get('type')=='function_call_output'))
            if metadata.get('captcha') or metadata.get('login_required') or not metadata.get('actions'):
                self.outputs=[[answer('La ruta requiere ayuda humana.')]]
            else:
                self.outputs=[[call('portal_browser_navigate',observation_id=metadata['observation_id'],
                    reference=metadata['actions'][0]['reference'])]]
        return await super().get_response(system_instructions,input,*args,**kwargs)


def events(caplog):
    return [json.loads(r.message) for r in caplog.records if r.name=='personal_agent.portal' and '"step"' in r.message]


def scenario(factory,case,*,extra='',**kwargs):
    def configure(app,ticket):
        app.state.billing.browser.pages.pages=dict(PAGES[case])
        app.state.billing.browser.pages.pages[ROOT]+=extra
    return prepared(factory,before_portal=configure,model=ObservingModel(),**kwargs)


def test_wizard_measures_the_login_barrier_instead_of_guessing_a_workaround(billing_client,caplog):
    caplog.set_level(logging.INFO,logger='personal_agent.portal')
    values,browser,model=scenario(billing_client,'wizard')
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app))
    record=app.state.billing.browser_review(owner(app),ticket['id'])
    measured=events(caplog)
    stopped=next(e for e in reversed(measured) if e['tool']=='telegram_portal' and e['phase']=='stop')
    assert stopped['reason']=='captcha_or_unfillable' and stopped['filter']=='login_required'
    assert stopped['step']==2 and stopped['elapsed_ms']>0
    assert [url for url,_ in browser.pages.calls]==[ROOT,ROOT+'access']
    assert browser.calls==['open','act'] and len(model.inputs)==3
    click=next(e for e in measured if e['action']=='click')
    assert click['role']=='button' and click['element_text']=='Continuar sin evaluar'
    assert click['path']=='/access' and click['fields_before']==click['fields_after']==0
    assert any(e['filter']=='not_billing_hint' for e in measured)
    assert 'Necesito tu ayuda' in telegram.messages[-1][1]
    # Ni siquiera una referencia válida permite saltar la barrera de login.
    before=list(browser.calls)
    action=record['snapshot']['actions'][0]
    with pytest.raises(AppError) as error:
        client.portal.call(partial(app.state.billing.browse_portal,owner(app),ticket['id'],
            observation_id=record['snapshot']['observation_id'],reference=action['reference']))
    assert error.value.code=='browser_intervention' and browser.calls==before
    assert mail.sent==sat.calls==[] and 'fill' not in browser.calls
    print('wizard:',json.dumps(stopped))


def test_form_visible_on_first_page_finishes_without_navigation_or_writing(billing_client,caplog):
    caplog.set_level(logging.INFO,logger='personal_agent.portal')
    values,browser,model=scenario(billing_client,'initial_form')
    client,app,extractor,mail,sat,telegram,chat=values
    measured=events(caplog)
    stopped=next(e for e in reversed(measured) if e['phase']=='stop')
    assert stopped['reason']=='form_found' and stopped['step']==1
    assert stopped['fields_after']==2 and browser.calls==['open']
    assert len(model.inputs)==1 and 'Formulario encontrado' in telegram.messages[-1][1]
    ticket=app.state.telegram.workflow.active(owner(app))
    record=app.state.billing.browser_review(owner(app),ticket['id'])
    page=browser.manager.sessions[record['snapshot']['session_id']].page
    assert client.portal.call(page.locator('input').evaluate_all,'nodes=>nodes.map(e=>e.value)')==['','']
    assert mail.sent==sat.calls==[]
    print('initial_form:',json.dumps(stopped))


@pytest.mark.parametrize('case',['captcha','unfillable'])
def test_verification_requires_human_help_and_never_fills_or_clicks(billing_client,caplog,case):
    caplog.set_level(logging.INFO,logger='personal_agent.portal')
    values,browser,model=scenario(billing_client,case)
    client,app,extractor,mail,sat,telegram,chat=values
    stopped=next(e for e in reversed(events(caplog)) if e['phase']=='stop')
    assert stopped['reason']=='captcha_or_unfillable'
    assert stopped['filter']==('captcha' if case=='captcha' else 'unsupported_required')
    assert 'Necesito tu ayuda' in telegram.messages[-1][1]
    assert not any(data.startswith(('xg:','lv:')) for _,data in telegram.messages[-1][2])
    assert browser.calls==['open'] and mail.sent==sat.calls==[]
    assert not any(item[0]=='fields' for item in extractor.calls)
    print(case+':',json.dumps(stopped))


@pytest.mark.parametrize('secret',[FISCAL['rfc'],'privado@example.test',FISCAL['name'],TICKET['folio'],TICKET['total']])
def test_log_validator_rejects_fiscal_data_instead_of_emitting_it(caplog,secret):
    clean=step_event({'page':ROOT,'fields':[]})
    contaminated=clean|{'element_text':secret}
    caplog.set_level(logging.INFO,logger='personal_agent.portal')
    with pytest.raises(ValueError,match='privado'):
        logging.getLogger('personal_agent.portal').info(json.dumps(validate_step(contaminated,[secret])))
    assert not caplog.records
    sanitized=step_event({'page':ROOT+'customer/'+secret+'?token=never-log','fields':[]},
        element={'diagnostic_label':'Continuar '+secret},private=[secret])
    assert secret not in json.dumps(sanitized) and '?' not in sanitized['path'] and 'never-log' not in json.dumps(sanitized)
    assert set(sanitized)==STEP_FIELDS


@pytest.mark.parametrize('attributes,image,expected',[
    ('aria-label="Acceso público" title="Secundario"','<img alt="Imagen" src="/archivo.svg?private=never-log">','Acceso público'),
    ('title="Acceso público"','<img alt="Imagen" src="/archivo.svg?private=never-log">','Acceso público'),
    ('','<img alt="Acceso público" src="/archivo.svg?private=never-log">','Acceso público'),
    ('','<img src="/acceso.svg?private=never-log">','acceso'),
    ('','','sin_nombre'),
])
def test_image_control_names_use_requested_order_in_the_initial_form_case(billing_client,attributes,image,expected):
    extra='<button type=button '+attributes+'>'+image+'</button>'
    values,browser,model=scenario(billing_client,'initial_form',extra=extra)
    client,app,*_=values
    ticket=app.state.telegram.workflow.active(owner(app))
    snapshot=app.state.billing.browser_review(owner(app),ticket['id'])['snapshot']
    action=next(a for a in snapshot['actions'] if a['role']=='button')
    assert action['diagnostic_label']==expected
    event=step_event(snapshot,element=action,action='click',before=snapshot)
    assert event['element_text']==expected and 'never-log' not in json.dumps(event)


def test_writing_logs_only_field_labels_and_rejects_a_contaminated_event(billing_client,caplog):
    from .test_telegram_agent import button
    caplog.set_level(logging.INFO,logger='personal_agent.portal')
    values,browser,model=scenario(billing_client,'initial_form')
    client,app,extractor,mail,sat,telegram,chat=values
    ticket=app.state.telegram.workflow.active(owner(app))
    caplog.clear()
    chat(data=button(telegram,'xg'))
    writes=[e for e in events(caplog) if e['action']=='write']
    assert [e['element_text'] for e in writes]==['RFC','Ticket']
    assert all(e['role']=='textbox' and len(e['element_text'])<=80 for e in writes)
    assert FISCAL['rfc'] not in caplog.text and FISCAL['name'] not in caplog.text and TICKET['folio'] not in caplog.text
    assert mail.sent==sat.calls==[]
    caplog.clear()
    contaminated=step_event({'page':ROOT,'fields':[]})|{'element_text':FISCAL['name']}
    with pytest.raises(ValueError,match='privado'):
        app.state.billing.portal_diagnostic(owner(app),ticket,tool='portal_fill',phase='navigation_step',navigation=contaminated)
    assert not caplog.records
