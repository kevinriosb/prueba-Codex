"""Permiso documental separado de recursos, sin necesitar un proceso Chromium."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest

from backend.browser_worker.engine import BrowserManager, Session
from backend.browser_worker.policy import BrowserError, PublicFetcher


ROOT='https://merchant.example'
ASSET='https://assets.example'


class Route:
    def __init__(self,session,url,kind,*,main=False,method='GET'):
        self.request=SimpleNamespace(url=url,resource_type=kind,method=method,
            frame=session.page.main_frame if main else object(),all_headers=AsyncMock(return_value={}))
        self.abort=AsyncMock()
        self.fulfill=AsyncMock()


def session(*,public_route=True):
    page=SimpleNamespace(main_frame=object(),reload=AsyncMock())
    return Session('owner',None,page,{ROOT},public_route=public_route,document_allowed={ROOT})


@pytest.mark.parametrize('kind',['script','stylesheet','xhr','fetch'])
async def test_new_auxiliary_origin_inherits_route_permission_but_not_document_permission(kind):
    fetcher=SimpleNamespace(fetch=AsyncMock(return_value=(200,{'content-type':'text/plain'},b'void 0;')))
    manager=BrowserManager(fetcher=fetcher)
    current=session()
    resource=Route(current,ASSET+'/resource',kind)
    await manager._route(current,resource)
    fetcher.fetch.assert_awaited_once()
    resource.fulfill.assert_awaited_once()
    resource.abort.assert_not_awaited()
    assert current.allowed=={ROOT,ASSET} and current.document_allowed=={ROOT}
    assert current.blocked==set() and not current.network_blocked


@pytest.mark.parametrize('main',[False,True],ids=['iframe','navigation'])
async def test_an_auto_allowed_resource_origin_does_not_authorize_its_documents(main):
    fetcher=SimpleNamespace(fetch=AsyncMock())
    manager=BrowserManager(fetcher=fetcher)
    current=session()
    assert manager._route_origin(current,ASSET)
    document=Route(current,ASSET+'/form','document',main=main)
    await manager._route(current,document)
    fetcher.fetch.assert_not_awaited()
    assert current.document_allowed=={ROOT} and current.blocked_documents=={ASSET}
    assert current.blocked=={ASSET}
    if main:
        assert current.network_blocked and current.block_reason=='permission'
        assert ASSET+'/form' in document.fulfill.await_args.kwargs['body']
    else:
        document.abort.assert_awaited_once()


async def test_exact_origin_permission_does_not_automatically_admit_auxiliary_origins():
    fetcher=SimpleNamespace(fetch=AsyncMock())
    manager=BrowserManager(fetcher=fetcher)
    current=session(public_route=False)
    resource=Route(current,ASSET+'/widget.js','script')
    await manager._route(current,resource)
    fetcher.fetch.assert_not_awaited()
    assert current.allowed==current.document_allowed=={ROOT} and current.blocked=={ASSET}


def test_auxiliary_origins_still_stop_at_twelve_without_expanding_document_permissions():
    manager=BrowserManager(fetcher=object())
    current=session()
    for index in range(11):
        assert manager._route_origin(current,'https://asset'+str(index)+'.example')
    with pytest.raises(BrowserError,match='browser_origin'):
        manager._route_origin(current,'https://thirteenth.example')
    assert len(current.allowed)==12 and current.document_allowed=={ROOT}
    assert current.unsafe_blocked and current.block_reason=='origin_limit'


async def test_allow_does_not_promote_automatic_resources_to_document_permissions():
    manager=BrowserManager(fetcher=object())
    current=session()
    manager.sessions['session']=current
    manager._observe=AsyncMock(return_value={})
    assert manager._route_origin(current,ASSET)
    current.blocked={ASSET}
    current.blocked_documents={ASSET}
    await manager.allow('owner','session',[ROOT],public_route=True)
    assert current.document_allowed=={ROOT} and current.blocked_documents=={ASSET}
    current.page.reload.assert_not_awaited()
    await manager.allow('owner','session',[ROOT,ASSET],public_route=True)
    assert current.document_allowed=={ROOT,ASSET} and current.blocked_documents==set()
    current.page.reload.assert_awaited_once()


@pytest.mark.parametrize('addresses',[['127.0.0.1'],['93.184.216.34','10.0.0.1']])
async def test_automatic_auxiliary_origin_still_cannot_fetch_private_or_mixed_dns(addresses):
    requests=[]
    async def resolver(_host):return addresses
    def transport(request):
        requests.append(request)
        pytest.fail('No debe consultar DNS privado o mixto.')
    fetcher=PublicFetcher(resolver=resolver,client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)))
    manager=BrowserManager(fetcher=fetcher)
    try:
        current=session()
        resource=Route(current,ASSET+'/widget.js','script')
        await manager._route(current,resource)
        assert requests==[]
        resource.abort.assert_awaited_once()
        resource.fulfill.assert_not_awaited()
    finally:
        await manager.close()


async def test_public_auxiliary_post_and_tracking_stay_blocked_without_http():
    fetcher=SimpleNamespace(fetch=AsyncMock())
    manager=BrowserManager(fetcher=fetcher)
    current=session()
    post=Route(current,ASSET+'/lookup','xhr',method='POST')
    tracker=Route(current,'https://www.googletagmanager.com/gtm.js','script')
    await manager._route(current,post)
    await manager._route(current,tracker)
    fetcher.fetch.assert_not_awaited()
    assert current.omitted_writes==1 and current.allowed==current.document_allowed=={ROOT}
    post.abort.assert_awaited_once()
    tracker.abort.assert_awaited_once()
