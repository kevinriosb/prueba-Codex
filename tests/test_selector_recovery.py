"""Marcas observadas y regreso al formulario propio; sin recetas por dominio."""
import json
import time

import pytest

from backend.app.billing.portal_navigation import next_public_action
from backend.app.billing.portal_memory import stable_form_bookmark
from backend.browser_worker.engine import BrowserManager
from .test_browser_worker import Pages
from .test_billing import billing_client
from .test_portal_form_completion import prepared,FORM
from .test_portal_preparation import preauthorize_fixture_origin
from .test_telegram_agent import button,owner
from .fake_model import call


ROOT='https://provider.example/brands'
DEST='https://provider.example/receipt?brand=Pizza'


@pytest.mark.parametrize('markup',[
    '<a href="'+DEST+'" style="display:block;width:80px;height:50px;background:#888"></a>',
    '<a href="'+DEST+'"><svg width="80" height="50"><title>Pizza Alfa</title><rect width="80" height="50"/></svg></a>',
    '<form><button type="button" onclick="location.href=\''+DEST+'\'"><img alt="Pizza Alfa"></button></form>',
])
async def test_brand_navigation_uses_observed_labels_and_destinations_without_clicking_handlers(markup):
    # El parámetro público de la marca es texto ya observado, no una URL inventada.
    dest=DEST.replace('Pizza','Pizza%20Alfa') if 'background' in markup else DEST
    markup=markup.replace(DEST,dest)
    pages=Pages({ROOT:'<h2>Selecciona la marca</h2>'+markup+
        '<footer><a href="https://group.example/">Facturación Electrónica</a></footer>',dest:'<label>RFC<input></label>'})
    manager=BrowserManager(fetcher=pages)
    try:
        first=await manager.open('owner',ROOT,['https://provider.example'],public_route=True)
        choice=next_public_action({'merchant':'Pizza Alfa'},first)
        assert choice is not None
        result=await manager.act('owner',first['session_id'],first['observation_id'],choice['reference'])
        assert result['fields'][0]['label']=='RFC'
        assert [url for url,_ in pages.calls]==[ROOT,dest]
    finally:await manager.close()


async def test_delayed_brand_controls_are_observed_before_returning_an_empty_selector():
    html='<h2>Selecciona la marca</h2><section id="brands"></section><script>setTimeout(()=>{document.getElementById("brands").innerHTML='+json.dumps(
        '<a href="'+DEST+'"><img alt="Pizza Alfa"></a><a href="/coffee"><img alt="Café Beta"></a>')+'},250)</script>'
    pages=Pages({ROOT:html,DEST:'<label>RFC<input></label>'})
    manager=BrowserManager(fetcher=pages)
    try:
        first=await manager.open('owner',ROOT,['https://provider.example'],public_route=True)
        choice=next_public_action({'merchant':'Pizza Alfa'},first)
        assert choice is not None
        result=await manager.act('owner',first['session_id'],first['observation_id'],choice['reference'])
        assert result['fields'][0]['label']=='RFC'
    finally:await manager.close()


def test_a_found_form_survives_expired_session_and_requires_fresh_permission(billing_client):
    values,model=prepared(billing_client,turns=1)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'wa'))
    billing=app.state.billing;user=owner(app)
    ticket=billing.store.list('ticket',user)[0]
    ticket['portal_checkpoint']['expires']=time.time()-1
    billing.store.update('ticket',user,ticket['id'],ticket)
    # El permiso del proveedor sigue vigente: reanudar en su formulario lo exige. Caduca el
    # del comercio, que es el que obliga a pedir permiso nuevo.
    preauthorize_fixture_origin(app,ticket,'https://provider.example')
    provider_lookup=billing.store.digest('portal-grant:'+billing.portal_permissions.scope(ticket)+':https://provider.example')
    with billing.database.connection(True) as conn:
        conn.execute("UPDATE billing_records SET expires=0 WHERE kind IN ('browser_review','portal_grant') "
            "AND NOT (kind='portal_grant' AND lookup=?)",(provider_lookup,))
    before=len(model.inputs);calls=list(billing.browser.calls)
    chat(data='bn:'+ticket['id'])
    assert billing.browser.calls==calls and len(model.inputs)==before
    model.outputs=[[call('portal_browser_observe')]]
    chat(data=button(telegram,'wa'))
    assert billing.browser.calls[-1][2]==FORM
    assert len(extractor.calls)==2 and mail.sent==sat.calls==[]
    assert len(billing.store.list('profile',user))==1


def test_form_checkpoint_is_scoped_and_an_unresolved_selector_does_not_overwrite_it(billing_client):
    values,_=prepared(billing_client,turns=1)
    client,app,extractor,mail,sat,telegram,chat=values
    chat(data=button(telegram,'wa'))
    billing=app.state.billing;user=owner(app)
    ticket=billing.store.list('ticket',user)[0]
    saved=dict(ticket['portal_form_checkpoint'])
    assert billing.portal_form_url(user,ticket)==FORM
    for other in (ticket|{'id':'different'},ticket|{'profile_id':'different'},ticket|{'billing_url':'https://different.example/'}):
        assert billing.portal_form_url(user,other) is None
    assert billing.portal_form_url('another-user',ticket) is None
    assert billing.portal_form_url(user,ticket|{'portal_form_checkpoint':saved|{'expires':0}}) is None
    current=billing.browser_review(user,ticket['id'])['snapshot']
    selector=current|{'page':ROOT,'bookmark_url':ROOT,'fields':[],'brand_selection':True}
    billing._save_portal_checkpoint(user,ticket,selector,None)
    assert ticket['portal_form_checkpoint']==saved and billing.portal_resume_url(user,ticket)==FORM
    blocked=current|{'network_blocked':True}
    assert stable_form_bookmark(ticket,blocked) is None
    for url in (FORM+'&token=private',FORM+'&folio=T-1234'):
        altered=current|{'fields':[dict(f,source_url=url) for f in current['fields']]}
        assert stable_form_bookmark(ticket,altered) is None


@pytest.mark.parametrize('readable_options',[False,True])
def test_unresolved_selector_finishes_in_one_turn_without_claiming_a_form_or_limit(billing_client,readable_options):
    from .test_adaptive_browser import FakeBrowser,snapshot
    from .test_telegram_agent import create
    from .test_telegram_documents import confirm_profile
    from .billing_fakes import image
    from .fake_model import ScriptedModel
    class Unresolved(FakeBrowser):
        async def open(self,*args,**kwargs):
            self.calls.append(('open',args[1]))
            actions=[{'reference':'bbbbbbbb','kind':'link','label':'Facturación del grupo'}]
            if readable_options:
                actions.extend([{'reference':'cccccccc','kind':'link','label':'Café Beta','brand':True},
                    {'reference':'dddddddd','kind':'link','label':'Hamburguesas Gamma','brand':True}])
            return snapshot(actions=actions)|{'brand_selection':True}
    browser=Unresolved();model=ScriptedModel([[call('portal_browser_observe')]])
    client,app,extractor,mail,sat,telegram,chat=create(billing_client,model,browser=browser,max_agent_turns=1)
    confirm_profile(telegram,chat)
    extractor.ticket['billing_url']=ROOT
    telegram.files['receipt']=image('red');chat(document='receipt');chat(data=button(telegram,'an'))
    start=len(telegram.messages);chat(data=button(telegram,'wa'))
    assert len(model.inputs)==1 and len(browser.calls)==1
    messages=telegram.messages[start:]
    assert len(messages)==1
    assert ('Puedes elegir la marca' if readable_options else 'no se detectaron sus opciones') in messages[0][1]
    assert 'pausada' not in messages[0][1].casefold() and 'Formulario encontrado' not in messages[0][1]
    assert mail.sent==sat.calls==[]
