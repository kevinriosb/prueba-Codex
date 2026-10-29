import asyncio
from types import SimpleNamespace

import pytest

from backend.browser_worker.engine import BrowserManager
import backend.browser_worker.engine as engine


class Closing:
    def __init__(self):self.closed=0
    async def close(self):self.closed+=1
    async def stop(self):self.closed+=1


@pytest.mark.parametrize('failure',[RuntimeError('launch failed'),asyncio.CancelledError()])
async def test_failed_launch_stops_its_driver_before_another_start(monkeypatch,failure):
    fetcher=Closing();manager=BrowserManager(fetcher=fetcher)
    drivers=[]
    class Factory:
        async def start(self):
            driver=Closing();driver.chromium=SimpleNamespace(launch=self.launch)
            drivers.append(driver);return driver
        async def launch(self,**kwargs):
            if len(drivers)==1:raise failure
            return Closing()
    monkeypatch.setattr(engine,'async_playwright',Factory)
    with pytest.raises(type(failure)):
        await manager._start()
    assert drivers[0].closed==1 and manager.playwright is None and manager.browser is None
    await manager._start()
    await manager.close()
    assert drivers[1].closed==1 and fetcher.closed==1


async def test_context_close_error_still_closes_browser_driver_and_fetcher():
    fetcher=Closing();manager=BrowserManager(fetcher=fetcher)
    browser=Closing();driver=Closing()
    manager.browser=browser;manager.playwright=driver
    class Context:
        async def close(self):raise RuntimeError('context failed')
    cache=SimpleNamespace(clear=lambda:None)
    manager.sessions={'session':SimpleNamespace(context=Context(),cache=cache,request_pages={},closed=False)}
    with pytest.raises(RuntimeError,match='context failed'):
        await manager.close()
    assert browser.closed==driver.closed==fetcher.closed==1
    assert manager.browser is manager.playwright is None and manager.sessions=={}
