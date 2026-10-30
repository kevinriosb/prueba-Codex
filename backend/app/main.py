import asyncio
import secrets
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent_service import AgentService
from .audit import audit
from .auth import Auth, COOKIE_NAME, authenticated, check_csrf, csrf_for, digest
from .config import Settings
from .database import Database
from .errors import AppError
from .security import BodyLimitMiddleware
from .billing.api import create_billing_router
from .billing.service import BillingService
from .billing.tools import build_billing_tools
from .billing.telegram import TelegramBridge, telegram_router

FRONTEND = Path(__file__).resolve().parents[2] / 'frontend'


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class Login(Payload):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=256)


class Message(Payload):
    text: str = Field(min_length=1, max_length=8000)


class Decision(Payload):
    approved: bool = Field(strict=True)


class DeleteConfirmation(Payload):
    confirmed: Literal[True]


def create_app(settings=None, *, model=None, extra_tools=(), billing_extractor=None,
               mail_provider=None, portal_adapters=(), sat_verifier=None, telegram_client=None,portal_browser=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        database = await asyncio.to_thread(Database, settings.app_database_path, failed_run_floor_tokens=settings.failed_run_floor_tokens)
        auth = await asyncio.to_thread(Auth, database, settings)
        service = AgentService(settings, database, model=model, extra_tools=extra_tools)
        app.state.database, app.state.auth, app.state.service = database, auth, service
        billing=BillingService(settings,database,service,extractor=billing_extractor,mail=mail_provider,
            portals=portal_adapters,sat=sat_verifier,browser=portal_browser)
        app.state.billing=billing
        app.state.telegram=TelegramBridge(settings,billing,agent_service=service,client=telegram_client)
        billing.notifier=app.state.telegram.notify
        if billing.enabled:
            service.tools.extend(build_billing_tools(billing))
            service.agent=service.agent.clone(tools=service.tools)

        async def purge_documents():
            while True:
                await asyncio.sleep(300)
                if billing.enabled:
                    await asyncio.to_thread(billing.store.purge)

        cleanup=asyncio.create_task(purge_documents())
        async def track_mailboxes():
            while True:
                await asyncio.sleep(60)
                try:
                    await billing.poll_due()
                except Exception:
                    audit('billing_background_failed',code='tracking_error')
        tracking_task=asyncio.create_task(track_mailboxes())
        try:
            yield
        finally:
            cleanup.cancel()
            tracking_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup
            with suppress(asyncio.CancelledError):
                await tracking_task
            await billing.close()
            await app.state.telegram.client.close()
            await service.close()

    app = FastAPI(title='Agente personal', version='0.1.0', lifespan=lifespan,
        telemetry={'tracing': False, 'metrics': False, 'logs': False, 'operation_spans': False, 'auto_configure': False},
        docs_url='/docs' if settings.app_env == 'development' else None,
        redoc_url=None, openapi_url='/openapi.json' if settings.app_env == 'development' else None)
    app.state.settings = settings
    app.add_middleware(BodyLimitMiddleware,upload_maximum=settings.billing_max_upload_bytes)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.hosts)

    @app.middleware('http')
    async def security(request, call_next):
        request.state.request_id = str(uuid.uuid4())
        if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and request.url.path!='/integrations/telegram/webhook':
            origin = request.headers.get('origin', '')
            try:
                parsed = urlsplit(origin)
                valid_origin = parsed.scheme in {'http', 'https'} and parsed.netloc == request.headers.get('host') and not parsed.path and not parsed.query and not parsed.fragment
                expected_scheme = 'https' if settings.app_secure_cookies else request.url.scheme
                valid_origin = valid_origin and parsed.scheme == expected_scheme
            except ValueError:
                valid_origin = False
            if not valid_origin:
                response = JSONResponse({'error': {'code': 'origin', 'message': 'Origen de solicitud no autorizado.'}}, status_code=403)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers.update({
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
            'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store',
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
            'X-Request-ID': request.state.request_id,
        })
        if settings.app_secure_cookies:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.exception_handler(AppError)
    async def app_error(request, error):
        return JSONResponse({'error': {'code': error.code, 'message': error.message, 'request_id': getattr(request.state, 'request_id', '')}}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({'error': {'code': 'validation', 'message': 'Revisa los campos enviados y sus límites.'}}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        audit('request_failed', code='internal_error')
        return JSONResponse({'error': {'code': 'internal_error', 'message': 'No se pudo completar la solicitud.', 'request_id': getattr(request.state, 'request_id', '')}}, status_code=500)

    @app.get('/health')
    async def health(request: Request):
        try:
            request.app.state.database.health()
        except Exception:
            return JSONResponse({'status': 'unhealthy'}, status_code=503)
        return {'status': 'ok', 'version': '0.1.0'}

    @app.get('/')
    async def index():
        return FileResponse(FRONTEND / 'index.html')

    @app.get('/assets/{filename}')
    async def asset(filename: str):
        if filename not in {'app.js', 'styles.css','billing.js','billing.css'}:
            raise AppError(404, 'not_found', 'Archivo no encontrado.')
        return FileResponse(FRONTEND / filename)

    @app.post('/api/auth/login')
    async def login(payload: Login, request: Request):
        address = request.client.host if request.client else 'unknown'
        token = await request.app.state.auth.login(payload.username, payload.password.get_secret_value(), address)
        response = JSONResponse({'username': settings.app_login_username, 'csrf_token': csrf_for(token)})
        response.set_cookie(COOKIE_NAME, token, max_age=settings.app_session_hours * 3600, httponly=True, secure=settings.app_secure_cookies, samesite='strict', path='/')
        return response

    @app.get('/api/auth/session')
    async def current_session(request: Request, user=Depends(authenticated)):
        return {'username': user['username'], 'csrf_token': csrf_for(request.cookies[COOKIE_NAME])}

    @app.post('/api/auth/logout', dependencies=[Depends(check_csrf)])
    async def logout(request: Request, user=Depends(authenticated)):
        request.app.state.database.delete_session(digest(request.cookies[COOKIE_NAME]))
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE_NAME, path='/', secure=settings.app_secure_cookies, httponly=True, samesite='strict')
        audit('logout')
        return response

    @app.get('/api/config')
    async def config(request: Request, user=Depends(authenticated)):
        return {'model': settings.ai_model, 'ai_configured': request.app.state.service.configured,
            'max_input_chars': settings.max_input_chars, 'max_daily_runs': settings.max_daily_runs,
            'max_daily_tokens': settings.max_daily_tokens, 'tools': [tool.name for tool in request.app.state.service.tools],
            'usage': request.app.state.database.usage(user['id'])}

    @app.get('/api/conversations')
    async def conversations(request: Request, user=Depends(authenticated)):
        return {'conversations': request.app.state.database.list_conversations(user['id'])}

    @app.post('/api/conversations', dependencies=[Depends(check_csrf)])
    async def create_conversation(request: Request, user=Depends(authenticated)):
        row = request.app.state.database.create_conversation(user['id'], settings.max_conversations)
        return request.app.state.service.public_conversation(row['id'], user['id'])

    @app.get('/api/conversations/{identifier}')
    async def conversation(identifier: uuid.UUID, request: Request, user=Depends(authenticated)):
        return request.app.state.service.public_conversation(str(identifier), user['id'])

    @app.delete('/api/conversations/{identifier}', dependencies=[Depends(check_csrf)])
    async def delete_conversation(identifier: uuid.UUID, payload: DeleteConfirmation, request: Request, user=Depends(authenticated)):
        request.app.state.database.delete_conversation(str(identifier), user['id'])
        audit('conversation_deleted')
        return {'ok': True}

    @app.post('/api/conversations/{identifier}/messages', dependencies=[Depends(check_csrf)])
    async def message(identifier: uuid.UUID, payload: Message, request: Request, user=Depends(authenticated)):
        return await request.app.state.service.execute(str(identifier), user['id'], text=payload.text)

    @app.post('/api/conversations/{identifier}/approvals/{approval_id}', dependencies=[Depends(check_csrf)])
    async def approval(identifier: uuid.UUID, approval_id: uuid.UUID, payload: Decision, request: Request, user=Depends(authenticated)):
        return await request.app.state.service.execute(str(identifier), user['id'], approval_id=str(approval_id), approve=payload.approved)

    app.include_router(create_billing_router(Payload))
    app.include_router(telegram_router())
    return app
