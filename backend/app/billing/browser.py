"""Cliente del navegador aislado. Solo envía URL pública y propietario opaco."""
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx2
from pydantic import BaseModel, ConfigDict, Field

from ...browser_worker.policy import BrowserError, origin
from ..errors import AppError
from .portal_inspection import field_key


class PublicField(BaseModel):
    model_config=ConfigDict(extra='forbid')
    label:str=Field(max_length=100)
    kind:str=Field(max_length=20)
    required:bool
    options:list[str]=Field(max_length=25)


class PublicAction(BaseModel):
    model_config=ConfigDict(extra='forbid')
    reference:str=Field(pattern=r'^[a-f0-9]{8}$')
    label:str=Field(max_length=100)
    kind:str=Field(pattern=r'^(link|expand)$')
    permission_origin:str|None=Field(default=None,max_length=2048)


class Observation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    session_id:UUID
    observation_id:str=Field(pattern=r'^[a-f0-9]{8}$')
    page:str=Field(max_length=2048)
    title:str=Field(max_length=120)
    fields:list[PublicField]=Field(max_length=24)
    actions:list[PublicAction]=Field(max_length=24)
    captcha:bool
    blocked_origins:list[str]=Field(max_length=12)
    network_blocked:bool
    steps_remaining:int=Field(ge=0,le=8)
    public_only:bool
    purchase_validated:bool
    submitted:bool


class PortalBrowserClient:
    def __init__(self,settings,*,client=None):
        self.settings=settings
        self.client=client or httpx2.AsyncClient(timeout=15,follow_redirects=False,trust_env=False)

    @property
    def enabled(self):
        return self.settings.billing_browser_enabled

    async def close(self):
        await self.client.aclose()

    async def _call(self,path,payload,*,method='POST',observation=True):
        if not self.enabled:
            raise AppError(503,'browser_disabled','La navegación aún no está activada por el operador.')
        try:
            token=self.settings.billing_browser_token_file.read_text().strip()
            uri=urlsplit(self.settings.billing_browser_worker_url)
            if len(token)<40 or uri.scheme not in {'http','https'} or not uri.hostname or uri.username or uri.password or uri.query or uri.fragment:
                raise ValueError()
            response=await self.client.request(method,self.settings.billing_browser_worker_url.rstrip('/')+path,
                headers={'X-Browser-Key':token},**({'params':payload} if method=='GET' else {'json':payload}))
            if len(response.content)>65536:
                raise ValueError()
            if response.status_code!=200:
                data=response.json()
                allowed={'browser_origin','browser_stale','browser_steps','browser_session','browser_busy','browser_intervention'}
                if data.get('code') in allowed:
                    raise AppError(response.status_code,data['code'],{
                        'browser_origin':'Autoriza ese dominio con el botón Permitir en Telegram.',
                        'browser_stale':'La página cambió. Actualiza la revisión.',
                        'browser_steps':'Se alcanzó el límite de navegación. Revisa los campos encontrados.',
                        'browser_session':'La revisión venció. Inicia otra revisión.',
                        'browser_intervention':'Se bloqueó una operación de la página. Requiere intervención humana.',
                        'browser_busy':'El navegador está ocupado. Intenta más tarde.'}[data['code']])
                raise ValueError()
            if not observation:
                if response.json()!={'closed':True}:
                    raise ValueError()
                return None
            observation=Observation.model_validate(response.json()).model_dump(mode='json')
            if not observation['public_only'] or observation['submitted'] or observation['purchase_validated']:
                raise ValueError()
            for item in observation['fields']:
                item['key']=field_key(item['label'])
            return observation
        except AppError:
            raise
        except Exception:
            raise AppError(502,'browser_unavailable','No se pudo revisar la página pública. No se envió información fiscal.') from None

    async def open(self,owner,url,*,allowed):
        # Solo mejora HTTP a HTTPS en el mismo hostname; TLS debe ser válido.
        uri=urlsplit(url)
        if uri.scheme=='http' and uri.port in {None,80} and not uri.username and not uri.password:
            url=urlunsplit(('https',uri.hostname,uri.path or '/',uri.query,''))
        uri=urlsplit(url)
        # La fase pública no transmite identificadores incluidos en consultas/fragmentos del ticket.
        url=urlunsplit((uri.scheme,uri.netloc,uri.path or '/','',''))
        try:
            allowed=sorted({origin(value) for value in allowed})
            if not allowed or len(allowed)>12:
                raise BrowserError('browser_origin','Dominio no autorizado.')
            if origin(url) not in allowed:
                raise BrowserError('browser_origin','Dominio no autorizado.')
        except BrowserError:
            raise AppError(403,'browser_origin','Autoriza ese dominio con el botón Permitir en Telegram.') from None
        return await self._call('/v1/sessions',{'owner':owner,'url':url,'allowed':allowed})

    async def allow(self,owner,session_id,allowed):
        return await self._call('/v1/sessions/'+str(UUID(session_id))+'/origins',
            {'owner':owner,'allowed':allowed})

    async def close_session(self,owner,session_id):
        return await self._call('/v1/sessions/'+str(UUID(session_id)),{'owner':owner},
            method='DELETE',observation=False)

    async def act(self,owner,session_id,observation_id,reference):
        try:
            UUID(session_id)
        except ValueError:
            raise AppError(422,'browser_session','Revisión no válida.') from None
        return await self._call('/v1/sessions/'+session_id+'/actions',
            {'owner':owner,'observation_id':observation_id,'reference':reference})

    async def observe(self,owner,session_id):
        try:
            UUID(session_id)
        except ValueError:
            raise AppError(422,'browser_session','Revisión no válida.') from None
        return await self._call('/v1/sessions/'+session_id,{'owner':owner},method='GET')
