"""Caché por contexto, límites durante streaming y diagnóstico sin datos privados."""
import asyncio
import json

import httpx2
import pytest

from backend.browser_worker.engine import BrowserManager
from backend.browser_worker.policy import BrowserError,PublicFetcher,PAGE_BYTE_LIMIT,ROUTE_BYTE_LIMIT,RESPONSE_BYTE_LIMIT
from backend.browser_worker.resources import ResourceCache,PageResources
from scripts.diagnose_portal import diagnose
from .test_browser_worker import Pages,HOME,ALLOWED
from .test_browser_resource_audit import WeightedPages


@pytest.mark.parametrize('headers',[
    {},{'cache-control':'no-store, max-age=90'},{'cache-control':'private,max-age=90'},
    {'cache-control':'no-cache,max-age=90'},{'cache-control':'public,max-age=0'},
    {'cache-control':'public,max-age=90','set-cookie':'not-replayed=yes'},
    {'cache-control':'public,max-age=90','vary':'*'},
    {'cache-control':'max-age=1.5'},{'cache-control':'max-age="broken"'},
])
def test_only_explicit_fresh_cacheable_assets_are_reused(headers):
    cache=ResourceCache()
    cache.put(HOME,{},200,headers,b'public asset')
    assert cache.get(HOME,{}) is None and cache.size==0


def test_cache_matches_cookie_and_vary_without_sharing_context_or_exceeding_memory():
    cache=ResourceCache(maximum=12)
    headers={'cache-control':'public,max-age=90','vary':'Accept-Language'}
    cache.put(HOME,{'cookie':'a=1','accept-language':'es'},200,headers,b'12345678')
    assert cache.get(HOME,{'cookie':'a=2','accept-language':'es'}) is None
    assert cache.get(HOME,{'cookie':'a=1','accept-language':'en'}) is None
    assert ResourceCache().get(HOME,{'cookie':'a=1','accept-language':'es'}) is None
    assert cache.get(HOME,{'cookie':'a=1','accept-language':'es'})[2]==b'12345678'
    cache.put(HOME+'other',{},200,{'cache-control':'max-age=90'},b'abcdefgh')
    assert cache.size<=12 and cache.get(HOME,{'cookie':'a=1','accept-language':'es'}) is None
    cache.clear();assert cache.size==0


def test_cache_expires_and_does_not_serve_stale_variants(monkeypatch):
    from backend.browser_worker import resources
    clock=[100.0]
    monkeypatch.setattr(resources.time,'monotonic',lambda:clock[0])
    cache=ResourceCache()
    cache.put(HOME,{},200,{'cache-control':'max-age=2'},b'asset')
    assert cache.get(HOME,{})
    clock[0]+=3
    assert cache.get(HOME,{}) is None and cache.size==0


class ChunkStream(httpx2.AsyncByteStream):
    def __init__(self,count):self.count=count;self.read=0;self.closed=False
    async def __aiter__(self):
        for _ in range(self.count):
            self.read+=1
            yield b'x'*65536
            await asyncio.sleep(0)
    async def aclose(self):self.closed=True


@pytest.mark.parametrize('limit,expected',[(1_048_576,'page_size_limit'),(RESPONSE_BYTE_LIMIT+65536,'browser_response_size')])
async def test_streaming_stops_at_budget_before_reading_the_entire_resource(limit,expected):
    stream=ChunkStream(160)
    async def resolver(_):return ['93.184.216.34']
    def transport(_):return httpx2.Response(200,stream=stream)
    fetcher=PublicFetcher(resolver=resolver,client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)))
    charged=[0]
    def consume(size):
        charged[0]+=size
        if charged[0]>limit:raise BrowserError('page_size_limit','test budget')
    try:
        with pytest.raises(BrowserError,match=expected):
            await fetcher.fetch_limited(HOME,{},on_bytes=consume)
        assert stream.read<160 and stream.closed
        assert charged[0]<=min(limit,RESPONSE_BYTE_LIMIT)+65536
    finally:await fetcher.close()


async def test_slow_drip_response_has_absolute_deadline_and_closes_stream(monkeypatch):
    from backend.browser_worker import policy
    monkeypatch.setattr(policy,'FETCH_TIMEOUT',.04)
    class Drip(ChunkStream):
        async def __aiter__(self):
            while True:
                self.read+=1
                yield b'x'
                await asyncio.sleep(.01)
    stream=Drip(1)
    async def resolver(_):return ['93.184.216.34']
    fetcher=PublicFetcher(resolver=resolver,client=httpx2.AsyncClient(transport=httpx2.MockTransport(
        lambda _:httpx2.Response(200,stream=stream))))
    try:
        with pytest.raises(TimeoutError):await fetcher.fetch(HOME,{})
        assert stream.closed and stream.read<20
    finally:await fetcher.close()


class HeavyPages(Pages):
    def __init__(self,pages,per_asset):
        self.per_asset=per_asset
        entries={}
        for step in range(pages):
            html=''.join('<script src="/asset/'+str(step)+'-'+str(n)+'.js"></script>' for n in range(3))
            html+=('<a href="/step/'+str(step+1)+'">Facturación</a>' if step<pages-1 else '<label>RFC<input></label>')
            entries[HOME if step==0 else HOME+'step/'+str(step)]=html
        super().__init__(entries)
    async def fetch(self,url,headers):
        if '/asset/' in url:
            self.calls.append((url,headers))
            return 200,{'content-type':'application/javascript','cache-control':'no-store'},b'/*'+b'x'*self.per_asset+b'*/'
        return await super().fetch(url,headers)


async def test_uncached_pages_can_exceed_old_aggregate_budget_without_exceeding_page_budget():
    manager=BrowserManager(fetcher=HeavyPages(4,1_000_000))
    try:
        current=await manager.open('owner',HOME,[ALLOWED[0]],public_route=True)
        for _ in range(3):
            current=await manager.act('owner',current['session_id'],current['observation_id'],current['actions'][0]['reference'])
        assert not current['network_blocked'] and current['fields'][0]['label']=='RFC'
        assert 6_291_456<current['diagnostics']['transferred_bytes']<ROUTE_BYTE_LIMIT
        assert current['diagnostics']['page_bytes']<PAGE_BYTE_LIMIT
    finally:await manager.close()


@pytest.mark.parametrize('pages,size,reason',[(1,3_000_000,'page_size_limit'),(5,2_000_000,'size_limit')])
async def test_heavy_pages_still_hit_precise_page_or_total_size_cap(pages,size,reason):
    manager=BrowserManager(fetcher=HeavyPages(pages,size))
    try:
        current=await manager.open('owner',HOME,[ALLOWED[0]],public_route=True)
        for _ in range(pages-1):
            if current['network_blocked']:break
            current=await manager.act('owner',current['session_id'],current['observation_id'],current['actions'][0]['reference'])
        assert current['network_blocked'] and current['block_reason']==reason
        assert any(issue['code']==reason for issue in current['diagnostics']['issues'])
    finally:await manager.close()


async def test_expired_page_resources_do_not_poison_next_page_budget():
    manager=BrowserManager(fetcher=Pages({HOME:'<p>public</p>'}))
    try:
        first=await manager.open('owner',HOME,[ALLOWED[0]],public_route=True)
        session=manager.sessions[first['session_id']]
        old_page=PageResources(size=PAGE_BYTE_LIMIT)
        with pytest.raises(BrowserError,match='page_size_limit'):
            manager._charge_bytes(session,old_page,1)
        assert not session.unsafe_blocked and not session.network_blocked
    finally:await manager.close()


async def test_large_resource_failure_is_reported_without_calling_document_illegible():
    async def resolver(_):return ['93.184.216.34']
    def transport(request):
        if request.url.path=='/big.js':
            return httpx2.Response(200,headers={'content-type':'application/javascript'},stream=ChunkStream(80))
        return httpx2.Response(200,headers={'content-type':'text/html'},text='<script src="/big.js"></script>')
    manager=BrowserManager(fetcher=PublicFetcher(resolver=resolver,client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport))))
    try:
        current=await manager.open('owner',HOME,[ALLOWED[0]],public_route=True)
        assert not current['network_blocked']
        assert any(issue['code']=='response_size_limit' and issue['kind']=='script' for issue in current['diagnostics']['issues'])
        assert 'cookie' not in json.dumps(current['diagnostics'])
    finally:await manager.close()


async def test_combined_diagnostic_follows_observed_route_without_ai_or_private_values():
    output=[]
    pages=WeightedPages()
    pages.pages[HOME+'step/2']=pages.pages[HOME+'step/2'].replace('<input>','<input value="never-print-fiscal">')
    code=await diagnose(HOME,manager=BrowserManager(fetcher=pages),emit=output.append)
    reports=[json.loads(line) for line in output if line.startswith('{')]
    assert code==0 and reports[-1]['fields'][0]['key']=='rfc'
    assert reports[-1]['diagnostics']['cache_hits']==10
    assert 'never-print-fiscal' not in '\n'.join(output)
