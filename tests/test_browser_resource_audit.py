"""Regresiones de recursos reales: peso, reutilización, cookies y DOM cambiante."""
import httpx2
import pytest

from backend.browser_worker.engine import BrowserManager
from backend.browser_worker.policy import BrowserError,PublicFetcher
from .test_browser_worker import Pages,HOME,FORM,ALLOWED


class WeightedPages(Pages):
    def __init__(self):
        super().__init__({HOME:self.page(1),HOME+'step/1':self.page(2),
            HOME+'step/2':self.page(None)})
    def page(self,next_step):
        assets=''.join('<script src="/shared-'+str(n)+'.js"></script>' for n in range(5))
        return assets+('<a href="/step/'+str(next_step)+'">Facturación</a>' if next_step else '<label>RFC<input></label>')
    async def fetch(self,url,headers):
        if '/shared-' in url:
            self.calls.append((url,headers))
            return 200,{'content-type':'application/javascript','cache-control':'public, max-age=3600'},b'/*'+b'x'*500_000+b'*/'
        return await super().fetch(url,headers)


async def test_repeated_public_assets_do_not_exhaust_route_before_final_form():
    pages=WeightedPages();manager=BrowserManager(fetcher=pages)
    try:
        current=await manager.open('owner',HOME,[ALLOWED[0]],public_route=True)
        for _ in range(2):
            current=await manager.act('owner',current['session_id'],current['observation_id'],current['actions'][0]['reference'])
        assert not current['network_blocked'] and current['fields'][0]['label']=='RFC'
        assert len([url for url,_ in pages.calls if '/shared-' in url])==5
    finally:
        await manager.close()


async def test_normal_application_bundle_over_one_megabyte_can_render_form():
    requests=[]
    async def resolver(host):return ['93.184.216.34']
    def transport(request):
        requests.append(request)
        if request.url.path=='/bundle.js':
            return httpx2.Response(200,headers={'content-type':'application/javascript'},
                content=b'/*'+b'x'*1_200_000+b'*/document.body.innerHTML="<label>Cuenta<input></label>";')
        return httpx2.Response(200,headers={'content-type':'text/html'},text='<body><script src="/bundle.js"></script>')
    fetcher=PublicFetcher(resolver=resolver,client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)))
    manager=BrowserManager(fetcher=fetcher)
    try:
        current=await manager.open('owner',HOME,[ALLOWED[0]],public_route=True)
        assert current['fields'][0]['label']=='Cuenta'
        assert len(requests)==2
    finally:
        await manager.close()


async def test_document_redirect_preserves_public_session_cookies_on_original_origin():
    class RedirectCookies(Pages):
        async def fetch(self,url,headers):
            self.calls.append((url,headers))
            if url==HOME:
                return 302,{'location':'/start','set-cookie':'a=one; Secure; Path=/\nb=two; HttpOnly; Secure; Path=/'},b''
            assert 'a=one' in headers.get('cookie','') and 'b=two' in headers.get('cookie','')
            return 200,{'content-type':'text/html'},b'<label>Cuenta<input></label>'
    manager=BrowserManager(fetcher=RedirectCookies({}))
    try:
        first=await manager.open('owner',HOME,[ALLOWED[0]],public_route=True)
        after=await manager.act('owner',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
        assert after['fields'][0]['label']=='Cuenta'
    finally:
        await manager.close()


async def test_fetcher_keeps_multiple_set_cookie_headers_separate():
    async def resolver(host):return ['93.184.216.34']
    def transport(request):
        return httpx2.Response(200,headers=[('set-cookie','a=one; Secure; Path=/'),('set-cookie','b=two; Secure; Path=/')],text='public')
    fetcher=PublicFetcher(resolver=resolver,client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)))
    try:
        _,headers,_=await fetcher.fetch(HOME,{})
        assert len(headers['set-cookie'].splitlines())==2
        assert len(fetcher.client.cookies)==0
    finally:
        await fetcher.close()


async def test_changed_link_is_refreshed_instead_of_executing_its_old_destination():
    pages=Pages({HOME:'<a id="go" href="'+FORM+'">Facturación</a>'})
    manager=BrowserManager(fetcher=pages)
    try:
        current=await manager.open('owner',HOME,ALLOWED)
        session=manager.sessions[current['session_id']]
        await session.page.locator('#go').evaluate("e=>e.href='/changed'")
        with pytest.raises(BrowserError,match='browser_stale'):
            await manager.act('owner',current['session_id'],current['observation_id'],current['actions'][0]['reference'])
        assert [url for url,_ in pages.calls]==[HOME]
    finally:
        await manager.close()


async def test_get_link_inside_outer_form_is_navigation_without_executing_submit_handler():
    pages=Pages({HOME:'<form method="post"><a href="'+FORM+'" onclick="fetch(\'/issue\',{method:\'POST\'})">Facturación</a><button>Emitir</button></form>',
        FORM:'<label>Cuenta<input></label>'})
    manager=BrowserManager(fetcher=pages)
    try:
        first=await manager.open('owner',HOME,ALLOWED)
        assert len(first['actions'])==1
        after=await manager.act('owner',first['session_id'],first['observation_id'],first['actions'][0]['reference'])
        assert after['fields'][0]['label']=='Cuenta'
        assert [url for url,_ in pages.calls]==[HOME,FORM]
    finally:
        await manager.close()


async def test_hidden_captcha_marker_does_not_block_public_form_observation():
    pages=Pages({HOME:'<div class="g-recaptcha" style="display:none"></div><label>RFC<input></label>'})
    manager=BrowserManager(fetcher=pages)
    try:
        current=await manager.open('owner',HOME,ALLOWED)
        assert not current['captcha'] and current['fields'][0]['label']=='RFC'
    finally:
        await manager.close()
