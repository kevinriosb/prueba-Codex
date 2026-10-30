"""Contratos separados de navegación pública, llenado aislado y envío autorizado."""
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID
from typing import Literal

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...browser_worker.policy import BrowserError, origin, bookmark_url
from ..errors import AppError
from .portal_inspection import field_key


class PublicField(BaseModel):
    model_config=ConfigDict(extra='forbid')
    label:str=Field(max_length=100)
    kind:str=Field(max_length=20)
    required:bool
    options:list[str]=Field(max_length=25)
    source_url:str|None=Field(default=None,max_length=2048)
    reference:str|None=Field(default=None,pattern=r'^[a-f0-9]{8}$')
    input_type:str=Field(default='text',max_length=25)
    editable:bool=True
    placeholder:str=Field(default='',max_length=100)
    max_length:int|None=Field(default=None,ge=0)


class FieldMeaning(BaseModel):
    model_config=ConfigDict(extra='forbid')
    reference:str=Field(pattern=r'^[a-f0-9]{8}$')
    key:Literal['rfc','name','postal_code','email','fiscal_regime','cfdi_use',
        'folio','account_number','reference_number','billing_code','web_id','store_number',
        'register_number','toll_station','lane','purchase_date','purchase_time','total','issuer_rfc']|None



class PublicAction(BaseModel):
    model_config=ConfigDict(extra='forbid')
    reference:str=Field(pattern=r'^[a-f0-9]{8}$')
    label:str=Field(max_length=100)
    kind:str=Field(pattern=r'^(link|expand)$')
    permission_origin:str|None=Field(default=None,max_length=2048)
    brand:bool=False


class ResourceIssue(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:str=Field(max_length=24)
    site:str=Field(max_length=2048)
    code:str=Field(max_length=40)
    status:int|None=Field(default=None,ge=100,le=599)


class BrowserDiagnostics(BaseModel):
    model_config=ConfigDict(extra='forbid')
    requests:int=Field(ge=0)
    page_requests:int=Field(ge=0)
    transferred_bytes:int=Field(ge=0)
    page_bytes:int=Field(ge=0)
    cache_hits:int=Field(ge=0)
    cache_bytes:int=Field(ge=0)
    issues:list[ResourceIssue]=Field(max_length=12)


class Observation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    session_id:UUID
    observation_id:str=Field(pattern=r'^[a-f0-9]{8}$')
    page:str=Field(max_length=2048)
    bookmark_url:str|None=Field(default=None,max_length=2048)
    title:str=Field(max_length=120)
    fields:list[PublicField]=Field(max_length=24)
    actions:list[PublicAction]=Field(max_length=48)
    captcha:bool
    blocked_origins:list[str]=Field(max_length=12)
    network_blocked:bool
    block_reason:str|None=Field(default=None,pattern=r'^(unsupported_request|unsupported_document|request_limit|page_request_limit|size_limit|page_size_limit|response_size_limit|origin_limit|navigation_failed|permission|timeout|network_error|dns_error|tls_error|private_address|http_error)$')
    omitted_writes:int=Field(default=0,ge=0)
    brand_selection:bool=False
    login_required:bool=False
    partial:bool=False
    diagnostics:BrowserDiagnostics|None=None
    unsupported_required:list[str]=Field(default_factory=list,max_length=10)
    requests_remaining:int|None=Field(default=None,ge=0,le=240)
    page_requests_remaining:int|None=Field(default=None,ge=0,le=60)
    steps_remaining:int=Field(ge=0,le=8)
    public_only:bool
    purchase_validated:bool
    submitted:bool


class FilledForm(BaseModel):
    model_config=ConfigDict(extra='forbid')
    session_id:UUID
    observation_id:str=Field(pattern=r'^[a-f0-9]{8}$')
    filled:bool
    filled_references:list[str]=Field(min_length=1,max_length=24)
    network_frozen:bool
    submitted:bool
    purchase_validated:bool
    expires_in:int=Field(ge=0,le=180)
    blocked_requests:int=Field(ge=0)


class TransactionAction(BaseModel):
    model_config=ConfigDict(extra='forbid')
    reference:str=Field(pattern=r'^[a-f0-9]{8}$')
    label:str=Field(max_length=100)
    kind:str=Field(pattern=r'^(continue|issue|download)$')
    origin:str=Field(max_length=2048)


class TransactionFile(BaseModel):
    model_config=ConfigDict(extra='forbid')
    reference:str=Field(pattern=r'^[a-f0-9]{8}$')
    mime:str=Field(pattern=r'^application/(xml|pdf)$')
    sha256:str=Field(pattern=r'^[a-f0-9]{64}$')


class TransactionView(BaseModel):
    model_config=ConfigDict(extra='forbid')
    snapshot:Observation
    actions:list[TransactionAction]=Field(max_length=12)
    error:str=Field(max_length=350)
    success:bool
    files:list[TransactionFile]=Field(max_length=6)


class TransactionResult(BaseModel):
    model_config=ConfigDict(extra='forbid')
    operation_id:UUID
    state:str=Field(pattern=r'^(unknown|correction|review|submitted)$')
    view:TransactionView


class PortalBrowserClient:
    def __init__(self,settings,*,client=None):
        self.settings=settings
        self.client=client or httpx2.AsyncClient(timeout=15,follow_redirects=False,trust_env=False)

    @property
    def enabled(self):
        return self.settings.billing_browser_enabled

    async def close(self):
        await self.client.aclose()

    async def _call(self,path,payload,*,method='POST',observation=True,filled=False):
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
                allowed={'browser_origin','browser_stale','browser_steps','browser_session','browser_busy','browser_intervention','browser_navigation','browser_fill','browser_private'}
                if data.get('code') in allowed:
                    raise AppError(response.status_code,data['code'],{
                        'browser_origin':'Autoriza ese dominio con el botón Permitir en Telegram.',
                        'browser_stale':'La página cambió. Actualiza la revisión.',
                        'browser_steps':'Se alcanzó el límite de navegación. Revisa los campos encontrados.',
                        'browser_session':'La revisión venció. Inicia otra revisión.',
                        'browser_navigation':'No se pudo abrir la página pública. Consulta el diagnóstico.',
                        'browser_fill':('Un campo no admite el dato revisado. Corrígelo antes de llenar.' if response.status_code==422 else
                            'No se completó el llenado; no se envió el formulario. Revisa los datos y el estado.'),
                        'browser_private':'La sesión contiene datos privados; no admite más navegación pública.',
                        'browser_intervention':'Se bloqueó una operación de la página. Requiere intervención humana.',
                        'browser_busy':'El navegador está ocupado. Intenta más tarde.'}[data['code']])
                raise ValueError()
            if filled:
                result=FilledForm.model_validate(response.json()).model_dump(mode='json')
                if (result['session_id']!=str(UUID(path.split('/')[3])) or not result['filled']
                        or not result['network_frozen'] or result['submitted'] or result['purchase_validated']):
                    raise AppError(502,'browser_boundary','El navegador no confirmó el llenado aislado. No se repetirá el paso.')
                return result
            if not observation:
                if response.json()!={'closed':True}:
                    raise ValueError()
                return None
            observation=Observation.model_validate(response.json()).model_dump(mode='json')
            if path.startswith('/v1/sessions/') and str(observation['session_id'])!=str(UUID(path.split('/')[3])):
                raise AppError(502,'browser_protocol','La respuesta no corresponde a la sesión solicitada. No se repetirá el paso.')
            if not observation['public_only'] or observation['submitted'] or observation['purchase_validated']:
                raise AppError(502,'browser_boundary','El navegador reportó una operación fuera del modo público. Revisa el estado antes de repetir.')
            for item in observation['fields']:
                item['key']=field_key(item['label'])
            return observation
        except AppError:
            raise
        except ValidationError:
            raise AppError(502,'browser_protocol','El agente y el navegador tienen contratos distintos. Actualiza ambos contenedores.') from None
        except Exception:
            message='No se confirmó el llenado. Consulta el estado antes de repetir.' if filled else 'No se pudo revisar la página pública. No se envió información fiscal.'
            raise AppError(502,'browser_unavailable',message) from None

    async def open(self,owner,url,*,allowed,public_route=False):
        # Solo mejora HTTP a HTTPS en el mismo hostname; TLS debe ser válido.
        uri=urlsplit(url)
        if uri.scheme=='http' and uri.port in {None,80} and not uri.username and not uri.password:
            url=urlunsplit(('https',uri.hostname,uri.path or '/',uri.query,''))
        uri=urlsplit(url)
        # Solo parámetros públicos de marca; no referencias de compra ni sesiones.
        url=bookmark_url(url) or urlunsplit((uri.scheme,uri.netloc,uri.path or '/','',''))
        try:
            allowed=sorted({origin(value) for value in allowed})
            if not allowed or len(allowed)>12:
                raise BrowserError('browser_origin','Dominio no autorizado.')
            if origin(url) not in allowed:
                raise BrowserError('browser_origin','Dominio no autorizado.')
        except BrowserError:
            raise AppError(403,'browser_origin','Autoriza ese dominio con el botón Permitir en Telegram.') from None
        return await self._call('/v1/sessions',{'owner':owner,'url':url,'allowed':allowed,
            **({'public_route':True} if public_route else {})})

    async def allow(self,owner,session_id,allowed,*,public_route=False):
        return await self._call('/v1/sessions/'+str(UUID(session_id))+'/origins',
            {'owner':owner,'allowed':allowed,**({'public_route':True} if public_route else {})})

    async def close_session(self,owner,session_id):
        return await self._call('/v1/sessions/'+str(UUID(session_id)),{'owner':owner},
            method='DELETE',observation=False)

    async def act(self,owner,session_id,observation_id,reference):
        try:
            session_id=str(UUID(session_id))
        except ValueError:
            raise AppError(422,'browser_session','Revisión no válida.') from None
        return await self._call('/v1/sessions/'+session_id+'/actions',
            {'owner':owner,'observation_id':observation_id,'reference':reference})

    async def observe(self,owner,session_id):
        try:
            session_id=str(UUID(session_id))
        except ValueError:
            raise AppError(422,'browser_session','Revisión no válida.') from None
        return await self._call('/v1/sessions/'+session_id,{'owner':owner},method='GET')

    async def fill(self,owner,session_id,observation_id,values):
        return await self._call('/v1/sessions/'+str(UUID(session_id))+'/fill',
            {'owner':owner,'observation_id':observation_id,'values':values},filled=True)

    async def filled(self,owner,session_id):
        return await self._call('/v1/sessions/'+str(UUID(session_id))+'/filled',{'owner':owner},method='GET',filled=True)

    async def _transaction_call(self,path,payload,*,method='GET',result=False,file=False):
        if not self.enabled:raise AppError(503,'browser_disabled','El navegador no está activado.')
        try:
            token=self.settings.billing_browser_token_file.read_text().strip()
            uri=urlsplit(self.settings.billing_browser_worker_url)
            if len(token)<40 or uri.scheme not in {'http','https'} or not uri.hostname or uri.username or uri.password or uri.query or uri.fragment:
                raise ValueError()
            response=await self.client.request(method,self.settings.billing_browser_worker_url.rstrip('/')+path,
                headers={'X-Browser-Key':token},timeout=25,**({'params':payload} if method=='GET' else {'json':payload}))
            if len(response.content)>(5_600_000 if file else 65536):raise ValueError()
            if response.status_code!=200:
                data=response.json()
                code=data.get('code')
                if code in {'browser_stale','browser_session','browser_private','browser_submission','browser_intervention'}:
                    raise AppError(response.status_code,code,{
                        'browser_stale':'Cambió el formulario. Revisa este paso antes de enviar.',
                        'browser_session':'La sesión venció. Revisa el estado; no repitas un envío sin confirmar.',
                        'browser_private':'Actualiza el formulario antes de solicitar la factura.',
                        'browser_submission':'El portal no admite este paso con los datos revisados.',
                        'browser_intervention':'El portal requiere intervención humana.'}[code])
                raise ValueError()
            if file:return response.json()
            data=(TransactionResult if result else TransactionView).model_validate(response.json()).model_dump(mode='json')
            view=data['view'] if result else data
            if view['snapshot']['session_id']!=str(UUID(path.split('/')[3])):raise ValueError()
            for item in view['snapshot']['fields']:item['key']=field_key(item['label'])
            return data
        except AppError:raise
        except Exception:
            raise AppError(502,'portal_send_unknown','No se confirmó la respuesta del portal. Consulta el estado; no repitas el envío.') from None

    async def transaction_view(self,owner,session_id):
        return await self._transaction_call('/v1/sessions/'+str(UUID(session_id))+'/transaction',{'owner':owner})

    async def transaction_send(self,owner,session_id,**payload):
        return await self._transaction_call('/v1/sessions/'+str(UUID(session_id))+'/transaction',{'owner':owner,**payload},method='POST',result=True)

    async def transaction_status(self,owner,session_id,operation_id):
        return await self._transaction_call('/v1/sessions/'+str(UUID(session_id))+'/transaction/'+str(UUID(operation_id)),{'owner':owner},result=True)

    async def transaction_file(self,owner,session_id,reference):
        if not re.fullmatch('[a-f0-9]{8}',reference):raise AppError(422,'portal_file','Archivo no válido.')
        return await self._transaction_call('/v1/sessions/'+str(UUID(session_id))+'/files/'+reference,{'owner':owner},file=True)
