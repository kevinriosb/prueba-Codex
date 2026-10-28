import json

import pytest

from backend.browser_worker.engine import BrowserManager
from backend.browser_worker.policy import BrowserError
from .test_browser_worker import Pages,HOME,ALLOWED


async def test_observed_fields_are_filled_with_network_closed_and_cannot_be_read_by_public_tools():
    pages=Pages({HOME:'''<form action="/issue" method="post">
        <label>RFC<input name="rfc"></label><label>Tienda<input name="store"></label>
        <label>Fecha<input type="date"></label><button>Enviar</button></form>
        <script>document.addEventListener('input',e=>{
          fetch('/leak?data='+encodeURIComponent(e.target.value)).catch(()=>{});
          fetch('/issue',{method:'POST',body:e.target.value}).catch(()=>{});
          document.querySelector('form').requestSubmit();
        });</script>'''})
    manager=BrowserManager(fetcher=pages)
    try:
        first=await manager.open('owner',HOME,ALLOWED,public_route=True)
        before=list(pages.calls)
        values=['AAA010101AAA','07001','2026-10-19']
        result=await manager.fill('owner',first['session_id'],first['observation_id'],[
            {'reference':item['reference'],'value':value} for item,value in zip(first['fields'],values)])
        assert result['filled'] and result['network_frozen'] and not result['submitted'] and not result['purchase_validated']
        session=await manager.get('owner',first['session_id'])
        await session.page.wait_for_timeout(100)
        assert await session.page.locator('input').evaluate_all('(nodes)=>nodes.map(e=>e.value)')==values
        assert pages.calls==before
        assert not any(value in json.dumps(result) for value in values)
        with pytest.raises(BrowserError) as private:
            await manager.observe('owner',first['session_id'])
        assert private.value.code=='browser_private'
        with pytest.raises(BrowserError):
            await manager.act('owner',first['session_id'],first['observation_id'],'12345678')
        with pytest.raises(BrowserError):
            await manager.fill('owner',first['session_id'],first['observation_id'],[{'reference':first['fields'][0]['reference'],'value':'OTHER'}])
        assert (await manager.filled('owner',first['session_id']))['filled']
    finally:await manager.close()


@pytest.mark.parametrize('change',["e.type='password'","e.name='other'","e.required=true","e.readOnly=true"])
async def test_changed_or_foreign_fields_do_not_receive_private_values(change):
    manager=BrowserManager(fetcher=Pages({HOME:'<label>RFC<input></label>'}))
    try:
        first=await manager.open('owner',HOME,ALLOWED)
        values=[{'reference':first['fields'][0]['reference'],'value':'AAA010101AAA'}]
        with pytest.raises(BrowserError) as foreign:
            await manager.fill('other',first['session_id'],first['observation_id'],values)
        assert foreign.value.status==404
        session=await manager.get('owner',first['session_id'])
        await session.page.locator('input').evaluate('e=>{'+change+'}')
        with pytest.raises(BrowserError) as stale:
            await manager.fill('owner',first['session_id'],first['observation_id'],values)
        assert stale.value.code=='browser_stale' and not session.private
        assert await session.page.locator('input').input_value()==''
    finally:await manager.close()


async def test_select_uses_a_unique_observed_option_and_rejects_unavailable_values_before_filling():
    manager=BrowserManager(fetcher=Pages({HOME:'<label>Régimen<select><option value="601">601 - General</option><option value="626">626 - Simplificado</option></select></label>'}))
    try:
        first=await manager.open('owner',HOME,ALLOWED)
        reference=first['fields'][0]['reference']
        with pytest.raises(BrowserError):
            await manager.fill('owner',first['session_id'],first['observation_id'],[{'reference':reference,'value':'999'}])
        result=await manager.fill('owner',first['session_id'],first['observation_id'],[{'reference':reference,'value':'626'}])
        assert result['filled']
        session=await manager.get('owner',first['session_id'])
        assert await session.page.locator('select').input_value()=='626'
    finally:await manager.close()


def test_internal_fill_api_requires_auth_owner_and_never_echoes_private_input_on_validation_errors(tmp_path):
    from fastapi.testclient import TestClient
    from backend.browser_worker.app import create_worker
    token=tmp_path/'token';token.write_text('t'*40)
    manager=BrowserManager(fetcher=Pages({HOME:'<label>RFC<input required></label>'}))
    with TestClient(create_worker(manager,token)) as client:
        headers={'X-Browser-Key':'t'*40}
        snapshot=client.post('/v1/sessions',headers=headers,json={'owner':'owner','url':HOME,'allowed':ALLOWED}).json()
        url='/v1/sessions/'+snapshot['session_id']+'/fill'
        payload={'owner':'owner','observation_id':snapshot['observation_id'],
            'values':[{'reference':snapshot['fields'][0]['reference'],'value':'PRIVATE-EXAMPLE'}]}
        assert client.post(url,json=payload).status_code==403
        invalid=client.post(url,headers=headers,json=payload|{'observation_id':'bad'})
        assert invalid.status_code==422 and 'PRIVATE-EXAMPLE' not in invalid.text
        assert client.post(url,headers=headers,json=payload|{'owner':'other'}).status_code==404
        result=client.post(url,headers=headers,json=payload)
        assert result.status_code==200 and result.json()['network_frozen']
        assert 'PRIVATE-EXAMPLE' not in result.text
        public=client.get('/v1/sessions/'+snapshot['session_id'],headers=headers,params={'owner':'owner'})
        assert public.status_code==409 and 'PRIVATE-EXAMPLE' not in public.text
        assert client.get(url+'ed',headers=headers,params={'owner':'other'}).status_code==404
        assert client.get(url+'ed',headers=headers,params={'owner':'owner'}).json()['filled']


async def test_required_fields_include_native_aria_and_visible_asterisks():
    manager=BrowserManager(fetcher=Pages({HOME:'<label>Tienda*<input></label><label>Fecha<input aria-required="true"></label><label>Correo<input required></label><label>Opcional<input></label>'}))
    try:
        snapshot=await manager.open('owner',HOME,ALLOWED)
        assert [f['required'] for f in snapshot['fields']]==[True,True,True,False]
    finally:await manager.close()
