import json
import socket
import ssl

import pytest

from backend.app.billing.browser import Observation
from backend.app.billing.portal_navigation import next_public_action
from backend.browser_worker.engine import BrowserManager
from backend.browser_worker.policy import BrowserError
from .test_browser_worker import Pages,HOME,FORM,ALLOWED


async def test_cache_is_context_and_cookie_bound_in_real_chromium():
    class CookieAssets(Pages):
        visits=0
        async def fetch(self,url,headers):
            self.calls.append((url,headers))
            if url.endswith('.js'):
                marker=headers.get('cookie','')
                return 200,{'content-type':'application/javascript','cache-control':'public,max-age=180'},('window.publicMarker='+json.dumps(marker)).encode()
            self.visits+=1
            return 200,{'content-type':'text/html','set-cookie':'public-session='+str(self.visits)+'; Secure; Path=/'},b'<script src="/public.js"></script><a href="/change">Facturaci\xc3\xb3n</a>'
    pages=CookieAssets({});manager=BrowserManager(fetcher=pages)
    try:
        first=await manager.open('owner-a',HOME,[ALLOWED[0]],public_route=True)
        changed=await manager.act('owner-a',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
        second=await manager.open('owner-b',HOME,[ALLOWED[0]],public_route=True)
        a=manager.sessions[changed['session_id']];b=manager.sessions[second['session_id']]
        assert a.cache is not b.cache
        assert await a.page.evaluate('window.publicMarker')=='public-session=2'
        assert await b.page.evaluate('window.publicMarker')=='public-session=3'
        assert len([url for url,_ in pages.calls if url.endswith('.js')])==3
    finally:await manager.close()


async def test_open_shadow_controls_and_form_fields_are_observed_without_values():
    markup='<a href="'+FORM+'">Facturación</a>'
    pages=Pages({HOME:'<div id="host"></div><script>document.getElementById("host").attachShadow({mode:"open"}).innerHTML='+json.dumps(markup)+'</script>',
        FORM:'<label>Cuenta<input value="not-exported"></label>'})
    manager=BrowserManager(fetcher=pages)
    try:
        first=await manager.open('owner',HOME,ALLOWED)
        assert first['actions'][0]['label']=='Facturación'
        after=await manager.act('owner',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
        assert after['fields'][0]['label']=='Cuenta' and 'not-exported' not in json.dumps(after)
    finally:await manager.close()


async def test_brand_beyond_first_twenty_four_controls_can_be_chosen_from_current_page():
    markup='<h1>Selecciona tu restaurante</h1>'
    for n in range(32):
        markup+='<a href="/brand/'+str(n)+'"><img alt="Restaurante Empresarial '+str(n)+'" src="/logo.png"></a>'
    markup+='<footer><a href="/start-again">Facturación Electrónica</a></footer>'
    pages=Pages({HOME:markup,HOME+'brand/31':'<label>RFC<input></label>'})
    manager=BrowserManager(fetcher=pages)
    try:
        first=await manager.open('owner',HOME,[ALLOWED[0]],public_route=True)
        assert len(first['actions'])>24
        Observation.model_validate(first)
        selected=next_public_action({'merchant':'Restaurante Empresarial 31'},first)
        after=await manager.act('owner',first['session_id'],first['observation_id'],selected['reference'])
        assert after['fields'][0]['label']=='RFC'
    finally:await manager.close()


async def test_large_public_observation_stays_inside_client_contract_and_marks_partial():
    url=HOME+'a'*1600
    options=''.join('<option>'+'texto-publico-'*5+'</option>' for _ in range(25))
    html=''.join('<label>Uso CFDI<select>'+options+'</select></label>' for _ in range(24))
    manager=BrowserManager(fetcher=Pages({url:html}))
    try:
        first=await manager.open('owner',url,[ALLOWED[0]],public_route=True)
        assert first['partial'] and len(json.dumps(first,ensure_ascii=False).encode())<=60000
        assert len(first['fields'])==24
        Observation.model_validate(first)
    finally:await manager.close()


@pytest.mark.parametrize('error,code',[(socket.gaierror('private diagnostic text'),'dns_error'),
    (ssl.SSLError('private diagnostic text'),'tls_error'),(TimeoutError('private diagnostic text'),'timeout')])
async def test_network_failures_report_precise_code_without_raw_exception(error,code):
    class Broken(Pages):
        async def fetch(self,*args):raise error
    manager=BrowserManager(fetcher=Broken({}))
    try:
        first=await manager.open('owner',HOME,[ALLOWED[0]],public_route=True)
        assert first['network_blocked'] and first['block_reason']==code
        assert 'private diagnostic text' not in json.dumps(first)
    finally:await manager.close()


async def test_http_error_and_script_failure_have_safe_diagnostics():
    class Denied(Pages):
        async def fetch(self,url,headers):
            return 403,{'content-type':'text/html'},b'<p>Forbidden</p>'
    manager=BrowserManager(fetcher=Denied({}))
    try:
        first=await manager.open('owner',HOME,[ALLOWED[0]],public_route=True)
        assert first['block_reason']=='http_error'
        assert first['diagnostics']['issues'][0]['status']==403
    finally:await manager.close()
    manager=BrowserManager(fetcher=Pages({HOME:'<script>throw new Error("never-export-error-body")</script>'}))
    try:
        first=await manager.open('owner',HOME,[ALLOWED[0]],public_route=True)
        assert any(issue['code']=='javascript_error' for issue in first['diagnostics']['issues'])
        assert 'never-export-error-body' not in json.dumps(first)
    finally:await manager.close()


async def test_visible_login_stops_navigation_instead_of_treating_it_as_invoice_form():
    pages=Pages({HOME:'<form><label>RFC<input></label><label>Contraseña<input type="password"></label></form><a href="'+FORM+'">Facturación</a>'})
    manager=BrowserManager(fetcher=pages)
    try:
        first=await manager.open('owner',HOME,ALLOWED)
        assert first['login_required']
        with pytest.raises(BrowserError,match='browser_intervention'):
            await manager.act('owner',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
    finally:await manager.close()


async def test_hidden_frames_do_not_displace_visible_billing_frame():
    html=''.join('<iframe src="/hidden/'+str(n)+'" style="display:none"></iframe>' for n in range(10))
    html+='<iframe src="'+FORM+'"></iframe>'
    manager=BrowserManager(fetcher=Pages({HOME:html,FORM:'<label>Cuenta<input></label>'}))
    try:
        current=await manager.open('owner',HOME,ALLOWED,public_route=True)
        assert current['fields'][0]['label']=='Cuenta'
        assert current['fields'][0]['source_url']==FORM
    finally:await manager.close()


async def test_inline_srcdoc_frame_uses_approved_parent_and_can_follow_observed_link():
    markup='<a href="'+FORM+'">Facturación</a>'
    import html
    manager=BrowserManager(fetcher=Pages({HOME:'<iframe srcdoc="'+html.escape(markup,quote=True)+'"></iframe>',FORM:'<label>Cuenta<input></label>'}))
    try:
        current=await manager.open('owner',HOME,ALLOWED,public_route=True)
        assert current['actions'][0]['label']=='Facturación'
        current=await manager.act('owner',current['session_id'],current['observation_id'],current['actions'][0]['reference'])
        assert current['fields'][0]['label']=='Cuenta'
    finally:await manager.close()


@pytest.mark.parametrize('attribute',['data-toggle','data-bs-toggle'])
async def test_tab_controls_can_reveal_form_without_submission(attribute):
    html='<button '+attribute+'="tab" onclick="document.getElementById(\'details\').hidden=false">Datos para facturación</button><div id="details" hidden><label>Cuenta<input></label></div>'
    manager=BrowserManager(fetcher=Pages({HOME:html}))
    try:
        first=await manager.open('owner',HOME,[ALLOWED[0]],public_route=True)
        assert not first['fields'] and first['actions'][0]['kind']=='expand'
        after=await manager.act('owner',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
        assert after['fields'][0]['label']=='Cuenta' and not after['submitted']
    finally:await manager.close()


@pytest.mark.parametrize('media',['application/pdf','application/json'])
async def test_non_html_document_is_reported_without_inventing_a_form(media):
    class FilePage(Pages):
        async def fetch(self,*args):return 200,{'content-type':media},b'not-a-form'
    manager=BrowserManager(fetcher=FilePage({}))
    try:
        first=await manager.open('owner',HOME,[ALLOWED[0]],public_route=True)
        assert first['block_reason']=='unsupported_document' and not first['fields']
    finally:await manager.close()
