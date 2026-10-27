import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .engine import BrowserManager
from .policy import BrowserError
from ..app.security import BodyLimitMiddleware


class Open(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)
    owner:str=Field(min_length=1,max_length=100)
    url:str=Field(max_length=2048)
    allowed:list[str]=Field(min_length=1,max_length=12)


class Action(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)
    owner:str=Field(min_length=1,max_length=100)
    observation_id:str=Field(pattern=r'^[a-f0-9]{8}$')
    reference:str=Field(pattern=r'^[a-f0-9]{8}$')


class Permission(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)
    owner:str=Field(min_length=1,max_length=100)
    allowed:list[str]=Field(min_length=1,max_length=12)


class Close(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)
    owner:str=Field(min_length=1,max_length=100)


def create_worker(manager=None,token_file=None):
    token_path=Path(token_file or os.environ.get('BROWSER_TOKEN_FILE','/run/browser-auth/token'))
    manager=manager or BrowserManager()
    @asynccontextmanager
    async def lifespan(app):
        app.state.token=token_path.read_text().strip()
        if len(app.state.token)<40:
            raise RuntimeError('Configura la credencial privada del navegador.')
        for name in ('httpx2','httpcore2'):
            logging.getLogger(name).setLevel(logging.CRITICAL+1)
        async def cleanup():
            while True:
                await asyncio.sleep(30)
                await manager.purge()
        task=asyncio.create_task(cleanup())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await manager.close()
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None,
        telemetry={'tracing':False,'metrics':False,'logs':False,'operation_spans':False,'auto_configure':False})
    app.add_middleware(BodyLimitMiddleware,maximum=16384)
    @app.middleware('http')
    async def authorize(request,call_next):
        if request.url.path!='/health' and not secrets.compare_digest(
                request.headers.get('x-browser-key','').encode(),app.state.token.encode()):
            return JSONResponse({'code':'browser_auth','message':'Acceso al navegador no autorizado.'},status_code=403)
        return await call_next(request)
    @app.exception_handler(BrowserError)
    async def browser_error(request,error):
        return JSONResponse({'code':error.code,'message':error.message},status_code=error.status)
    @app.exception_handler(Exception)
    async def unexpected(request,error):
        return JSONResponse({'code':'browser_failure','message':'No se completó la revisión pública. No se reintentó.'},status_code=502)
    @app.get('/health')
    async def health():
        return {'status':'ok','public_only':True}
    @app.post('/v1/sessions')
    async def open_session(payload:Open):
        return await manager.open(payload.owner,payload.url,payload.allowed)
    @app.get('/v1/sessions/{identifier}')
    async def observe(identifier:str,owner:str):
        return await manager.observe(owner,identifier)
    @app.post('/v1/sessions/{identifier}/actions')
    async def act(identifier:str,payload:Action):
        return await manager.act(payload.owner,identifier,payload.observation_id,payload.reference)
    @app.post('/v1/sessions/{identifier}/origins')
    async def allow(identifier:str,payload:Permission):
        return await manager.allow(payload.owner,identifier,payload.allowed)
    @app.delete('/v1/sessions/{identifier}')
    async def close(identifier:str,payload:Close):
        return await manager.close_session(payload.owner,identifier)
    return app


app=create_worker()
