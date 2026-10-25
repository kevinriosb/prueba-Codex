import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from email.utils import parseaddr
from email.policy import SMTP
from urllib.parse import quote, urlencode, urlsplit

import httpx2
from email_validator import EmailNotValidError, validate_email

from ..errors import AppError

SCOPES={'https://www.googleapis.com/auth/gmail.send','https://www.googleapis.com/auth/gmail.readonly'}


def normalized_email(value):
    try:
        return validate_email(value,check_deliverability=False,allow_smtputf8=False).normalized.lower()
    except (EmailNotValidError,TypeError):
        raise AppError(422,'invalid_email','Introduce una dirección de correo válida.') from None


@dataclass
class Attachment:
    name: str
    mime: str
    data: bytes


@dataclass
class Delivery:
    message_id: str
    thread_id: str


@dataclass
class Reply:
    identifier: str
    sender: str
    in_reply_to: str
    text: str = ''
    attachments: list[Attachment] = field(default_factory=list)


class GmailProvider:
    """OAuth 2.0 por usuario. No guarda tokens fuera de BillingStore ni registra cuerpos."""
    def __init__(self,settings,store,*,client=None):
        self.settings,self.store=settings,store
        self.client=client or httpx2.AsyncClient(timeout=15,follow_redirects=False)

    @property
    def configured(self):
        uri=urlsplit(self.settings.gmail_redirect_uri)
        return bool(self.settings.gmail_client_id and self.settings.gmail_client_secret.get_secret_value()
            and uri.hostname in self.settings.hosts and uri.path=='/api/billing/gmail/callback'
            and not uri.query and not uri.fragment and not uri.username
            and (uri.scheme=='https' or (self.settings.app_env=='development' and uri.scheme=='http' and uri.hostname in {'localhost','127.0.0.1'})))

    def account(self,owner):
        return self.store.find('gmail',owner,'account')

    def ready(self,owner):
        return self.configured and self.account(owner) is not None

    async def close(self):
        await self.client.aclose()

    async def _json(self,method,url,**kwargs):
        if urlsplit(url).hostname not in {'oauth2.googleapis.com','gmail.googleapis.com'}:
            raise AppError(503,'mail_destination','Destino de correo no permitido.')
        try:
            async with self.client.stream(method,url,**kwargs) as response:
                if not 200<=response.status_code<300:
                    raise AppError(502,'gmail_provider','Gmail no pudo completar la operación. Revisa la conexión antes de repetirla.')
                chunks,size=[],0
                async for chunk in response.aiter_bytes():
                    size+=len(chunk)
                    if size>12_000_000:
                        raise AppError(413,'gmail_size','La respuesta de Gmail supera el límite permitido.')
                    chunks.append(chunk)
                return json.loads(b''.join(chunks))
        except AppError:
            raise
        except Exception:
            raise AppError(502,'gmail_connection','No se pudo confirmar la respuesta de Gmail. No se reintentará automáticamente.') from None

    def connect(self,owner):
        if not self.configured:
            raise AppError(503,'gmail_configuration','Configura el cliente OAuth de Google y su URL de retorno.')
        if len(self.store.list('oauth_state',owner))>=5:
            raise AppError(429,'oauth_limit','Termina la conexión de Gmail pendiente o espera a que caduque.')
        state,verifier=secrets.token_urlsafe(32),secrets.token_urlsafe(64)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        self.store.add('oauth_state',owner,{'verifier':verifier},lookup=self.store.digest(state),expires=time.time()+600,state='pending')
        return 'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode({
            'client_id':self.settings.gmail_client_id,'redirect_uri':self.settings.gmail_redirect_uri,
            'response_type':'code','scope':' '.join(sorted(SCOPES)),'access_type':'offline',
            'prompt':'consent','state':state,'code_challenge':challenge,'code_challenge_method':'S256'})

    async def finish_connect(self,state,code):
        owner,pending=self.store.find_oauth_state(self.store.digest(state))
        self.store.claim('oauth_state',owner,pending['id'])
        token=await self._json('POST','https://oauth2.googleapis.com/token',data={
            'client_id':self.settings.gmail_client_id,'client_secret':self.settings.gmail_client_secret.get_secret_value(),
            'redirect_uri':self.settings.gmail_redirect_uri,'grant_type':'authorization_code',
            'code':code,'code_verifier':pending['verifier']})
        if not SCOPES.issubset(set(token.get('scope','').split())) or not token.get('refresh_token') or not token.get('access_token'):
            raise AppError(403,'gmail_scopes','Autoriza envío y lectura de Gmail para conectar el buzón.')
        profile=await self._json('GET','https://gmail.googleapis.com/gmail/v1/users/me/profile',headers={'Authorization':'Bearer '+token['access_token']})
        account={'email':normalized_email(profile['emailAddress']),'refresh_token':token['refresh_token'],
            'access_token':token['access_token'],'expires':time.time()+min(int(token.get('expires_in',3600)),3600)}
        existing=self.account(owner)
        if existing:
            self.store.update('gmail',owner,existing['id'],account)
        else:
            self.store.add('gmail',owner,account,lookup='account')
        self.store.delete('oauth_state',owner,pending['id'])
        return owner

    async def _authorization(self,owner):
        account=self.account(owner)
        if not self.configured or not account:
            raise AppError(503,'gmail_not_connected','Conecta tu buzón de Gmail antes de continuar.')
        if account['expires']<time.time()+60:
            token=await self._json('POST','https://oauth2.googleapis.com/token',data={
                'client_id':self.settings.gmail_client_id,'client_secret':self.settings.gmail_client_secret.get_secret_value(),
                'refresh_token':account['refresh_token'],'grant_type':'refresh_token'})
            if not token.get('access_token'):
                raise AppError(503,'gmail_expired','Vuelve a conectar Gmail.')
            account['access_token']=token['access_token']
            account['expires']=time.time()+min(int(token.get('expires_in',3600)),3600)
            self.store.update('gmail',owner,account['id'],account,version=account['_version'])
        return {'Authorization':'Bearer '+account['access_token']}

    async def send(self,owner,message,*,thread_id=None):
        headers=await self._authorization(owner)
        raw=base64.urlsafe_b64encode(message.as_bytes(policy=SMTP)).decode()
        if len(raw)>12_000_000:
            raise AppError(413,'email_size','Los adjuntos superan el límite permitido.')
        payload={'raw':raw}
        if thread_id:
            payload['threadId']=self._identifier(thread_id)
        response=await self._json('POST','https://gmail.googleapis.com/gmail/v1/users/me/messages/send',headers=headers,json=payload)
        if not response.get('id') or not response.get('threadId'):
            raise AppError(502,'mail_unknown','Gmail no confirmó el identificador del envío.')
        return Delivery(response['id'],response['threadId'])

    @staticmethod
    def _identifier(value):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',value):
            raise AppError(422,'mail_identifier','Identificador de correo no válido.')
        return value

    @staticmethod
    def _attachment_identifier(value):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_+=/-]{1,4096}',value):
            raise AppError(422,'mail_attachment_identifier','Identificador de adjunto no válido.')
        return quote(value,safe='')

    async def fetch(self,owner,ticket):
        headers=await self._authorization(owner)
        thread=self._identifier(ticket['delivery']['thread_id'])
        data=await self._json('GET',f'https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread}?format=full',headers=headers)
        replies=[]
        for item in data.get('messages',[])[-20:]:
            if item.get('id')==ticket['delivery']['message_id']:
                continue
            payload=item.get('payload',{})
            metadata={head['name'].lower():head['value'] for head in payload.get('headers',[])}
            reference=metadata.get('in-reply-to','') or metadata.get('references','')
            permitted=ticket['delivery'].get('request_message_ids',[ticket['delivery']['rfc_message_id']])
            matched=next((identifier for identifier in permitted if identifier in reference.split()),None)
            if not matched:
                continue
            reply=Reply(self._identifier(item['id']),parseaddr(metadata.get('from',''))[1],matched)
            parts=[payload]
            total=0
            while parts:
                part=parts.pop()
                parts.extend(part.get('parts',[])[:10])
                body=part.get('body',{})
                if int(body.get('size',0))>8_388_608:
                    continue
                encoded=body.get('data','')
                if body.get('attachmentId'):
                    attachment_id=self._attachment_identifier(body['attachmentId'])
                    message_id=self._identifier(item['id'])
                    loaded=await self._json('GET',f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/attachments/{attachment_id}',headers=headers)
                    encoded=loaded.get('data','')
                if not encoded:
                    continue
                content=base64.urlsafe_b64decode(encoded+'='*((-len(encoded))%4))
                total+=len(content)
                if total>8_388_608:
                    raise AppError(413,'reply_size','Los archivos de la respuesta superan el límite.')
                mime=part.get('mimeType','')
                if mime=='text/plain' and not part.get('filename'):
                    reply.text+=content.decode('utf-8',errors='replace')[:8000]
                elif len(reply.attachments)<6 and (mime in {'application/pdf','application/xml','text/xml'} or part.get('filename','').lower().endswith('.xml')):
                    reply.attachments.append(Attachment('factura.xml' if mime!='application/pdf' else 'factura.pdf',mime,content))
            replies.append(reply)
        return replies
