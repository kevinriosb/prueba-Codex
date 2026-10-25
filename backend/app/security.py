import asyncio

from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    """Acota también cuerpos fragmentados, antes de que FastAPI los almacene."""
    def __init__(self, app, maximum=24000, upload_maximum=8_388_608):
        self.app, self.maximum, self.upload_maximum = app, maximum, upload_maximum

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            return await self.app(scope, receive, send)
        maximum=self.maximum
        if scope.get('path')=='/api/billing/documents' and scope['method']=='POST':
            from http.cookies import SimpleCookie
            from .auth import COOKIE_NAME, digest
            headers=dict(scope.get('headers',[]))
            cookies=SimpleCookie()
            try:
                cookies.load(headers.get(b'cookie',b'').decode())
                token=cookies[COOKIE_NAME].value if COOKIE_NAME in cookies else ''
                valid=bool(token and len(token)<=128 and scope['app'].state.database.session_user(digest(token)))
            except Exception:
                valid=False
            if not valid:
                response=JSONResponse({'error':{'code':'authentication_required','message':'Inicia sesión antes de subir documentos.'}},status_code=401)
                return await response(scope,receive,send)
            maximum=self.upload_maximum
        chunks, size = [], 0
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    data = message.get('body', b'')
                    size += len(data)
                    if size > maximum:
                        response = JSONResponse({'error': {'code': 'body_limit', 'message': 'La solicitud es demasiado grande.'}}, status_code=413)
                        return await response(scope, receive, send)
                    chunks.append(data)
                    if not message.get('more_body', False):
                        break
        except TimeoutError:
            response = JSONResponse({'error': {'code': 'body_timeout', 'message': 'La solicitud tardó demasiado.'}}, status_code=408)
            return await response(scope, receive, send)
        body = b''.join(chunks)
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()

        await self.app(scope, bounded_receive, send)
