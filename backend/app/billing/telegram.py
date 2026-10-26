import asyncio
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

import httpx2
from fastapi import APIRouter, Request

from ..errors import AppError
from .models import CFDI_USES, missing_fiscal_fields, validate_fiscal
from .extraction import EXTRACTION_VERSION
from .receipt import missing_receipt_fields
from .telegram_agent import TelegramAgent
from .telegram_profiles import TelegramProfiles, normalized


class TelegramClient:
    def __init__(self,settings,*,client=None):
        self.settings=settings
        self.client=client or httpx2.AsyncClient(timeout=20,follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def _call(self,method,**kwargs):
        token=self.settings.telegram_bot_token.get_secret_value()
        if not token or any(character in token for character in '/?#\r\n'):
            raise AppError(503,'telegram_configuration','Configura el token de Telegram en el servidor.')
        try:
            response=await self.client.post(f'https://api.telegram.org/bot{token}/{method}',**kwargs)
            if response.status_code!=200 or len(response.content)>100_000:
                raise ValueError()
            data=response.json()
            if not data.get('ok'):
                raise ValueError()
            return data.get('result')
        except Exception:
            raise AppError(502,'telegram_provider','Telegram no confirmó la operación. No se reintentará automáticamente.') from None

    async def send(self,chat_id,text,buttons=None):
        payload={'chat_id':chat_id,'text':text[:4000],'link_preview_options':{'is_disabled':True}}
        if buttons:
            payload['reply_markup']={'inline_keyboard':[[{'text':label,'callback_data':data}] for label,data in buttons]}
        await self._call('sendMessage',json=payload)

    async def download(self,file_id):
        metadata=await self._call('getFile',json={'file_id':file_id})
        path=metadata.get('file_path','')
        parsed=PurePosixPath(path)
        if not path or parsed.is_absolute() or '..' in parsed.parts or any(char in path for char in '\\%?#\r\n'):
            raise AppError(422,'telegram_file','Telegram devolvió una ruta de archivo no permitida.')
        if metadata.get('file_size',0)>self.settings.billing_max_upload_bytes:
            raise AppError(413,'document_size','El documento supera el límite permitido.')
        token=self.settings.telegram_bot_token.get_secret_value()
        try:
            async with self.client.stream('GET',f'https://api.telegram.org/file/bot{token}/{path}') as response:
                if response.status_code!=200:
                    raise ValueError()
                chunks,size=[],0
                async for chunk in response.aiter_bytes():
                    size+=len(chunk)
                    if size>self.settings.billing_max_upload_bytes:
                        raise AppError(413,'document_size','El documento supera el límite permitido.')
                    chunks.append(chunk)
                return b''.join(chunks)
        except AppError:
            raise
        except Exception:
            raise AppError(502,'telegram_file','No se pudo recibir el archivo de Telegram.') from None

    async def send_document(self,chat_id,document):
        import base64
        extension={'application/xml':'xml','application/pdf':'pdf','image/jpeg':'jpg'}[document['mime']]
        await self._call('sendDocument',data={'chat_id':str(chat_id)},files={
            'document':('factura.'+extension,base64.b64decode(document['data']),document['mime'])})


class TelegramBridge:
    def __init__(self,settings,billing,*,agent_service=None,client=None):
        self.settings,self.billing=settings,billing
        self.client=client or TelegramClient(settings)
        try:
            self.allowed={int(value.strip()) for value in settings.telegram_allowed_user_ids.split(',') if value.strip()}
        except ValueError:
            self.allowed=set()
        self.agent=TelegramAgent(agent_service,self) if agent_service else None
        self.profiles=TelegramProfiles(self)
        self._locks={}

    @property
    def configured(self):
        return bool(self.billing.enabled and self.allowed and self.settings.telegram_bot_token.get_secret_value()
            and len(self.settings.telegram_webhook_secret.get_secret_value())>=32)

    def _state(self,owner):
        record=self.billing.store.find('channel_state',owner,'telegram')
        return record or self.billing.store.add('channel_state',owner,{'active_profile':None,'draft_id':None,'cfdi_use':None},lookup='telegram')

    def _save_state(self,owner,state):
        return self.billing.store.update('channel_state',owner,state['id'],state)

    def _profile_setup(self,owner,state,documents=None):
        draft=None
        if state.get('draft_id'):
            try:
                draft=self.billing.store.get('profile_draft',owner,state['draft_id'])
            except AppError as error:
                if error.status!=404:
                    raise
        documents=documents if documents is not None else self.billing.store.list('document',owner)
        profiles=self.billing.store.list('profile',owner)
        confirmed_documents={item.get('document_id') for item in profiles}
        identifiers=[state.get('csf_document_id'),(draft or {}).get('document_id')]
        if not profiles:
            identifiers.extend(item['id'] for item in documents if item['purpose']=='csf' and not item.get('superseded_by') and not item.get('intended_purpose'))
        for identifier in dict.fromkeys(identifiers):
            if not identifier:
                continue
            if not draft and identifier in confirmed_documents:
                continue
            try:
                document=self.billing.document(owner,identifier)
            except AppError as error:
                if error.status==404:
                    continue
                raise
            extracted=document.get('extracted')
            if document['purpose']=='csf' and not document.get('superseded_by') and not document.get('intended_purpose') and (extracted is None or extracted.get('is_fiscal_certificate')):
                return draft,document
        return draft,None

    def agent_context(self,owner):
        state=self._state(owner)
        profiles=self.billing.store.list('profile',owner)
        documents=[item for item in self.billing.store.list('document',owner)
            if item['purpose'] in {'csf','ticket','unclassified'} and not item.get('superseded_by')]
        last=next((item for item in documents if item['id']==state.get('last_document_id')),None)
        last_ticket=None
        if last and last.get('ticket_id'):
            last_ticket=self.billing.store.get('ticket',owner,last['ticket_id'])
        draft,csf=self._profile_setup(owner,state,documents)
        return {'profile_confirmed':bool(profiles),'active_profile_id':state.get('active_profile') if any(item['id']==state.get('active_profile') for item in profiles) else None,
            'csf_received':bool(profiles or draft or csf),'profile_setup_pending':bool(draft or csf),
            'draft_id':draft['id'] if draft else None,'csf_document_id':csf['id'] if csf else None,
            'missing_fiscal_fields':missing_fiscal_fields(draft) if draft else [],
            'last_document':{'id':last['id'],'purpose':last.get('intended_purpose') or last['purpose'],
                'analysis_error':(last.get('analysis_error') or {}).get('code'),
                'ticket_status':last_ticket['status'] if last_ticket else None,
                'awaiting_profile':last.get('intended_purpose')=='ticket'} if last else None,
            'pending_ticket_count':sum(item.get('intended_purpose')=='ticket' for item in documents)}

    async def _document_choice(self,owner,chat_id,identifier,*,reason=None):
        document=self.billing.document(owner,identifier)
        if document['purpose'] not in {'csf','ticket','unclassified'}:
            raise AppError(422,'document_purpose','Elige un archivo de CSF o ticket recibido en este chat.')
        await self.client.send(chat_id,(reason+'\n\n' if reason else '')+
            'Archivo guardado. ¿CSF o ticket? Elegir el tipo no usa IA.',
            [('Es una CSF','dc:'+identifier),('Es un ticket','dt:'+identifier)])

    async def _choose_document(self,owner,chat_id,identifier,purpose,*,profile_id=None):
        document=self.billing.document(owner,identifier)
        if document['purpose'] not in {'csf','ticket','unclassified'}:
            raise AppError(422,'document_purpose','No se puede cambiar el tipo de este archivo.')
        state=self._state(owner)
        if purpose=='ticket':
            if state.get('csf_document_id')==identifier:
                state['csf_document_id']=None
            profile_id=state.get('active_profile')
            profiles=self.billing.store.list('profile',owner)
            if not any(item['id']==profile_id for item in profiles) and len(profiles)==1:
                profile_id=profiles[0]['id']
                state['active_profile']=profile_id
            if not any(item['id']==profile_id for item in profiles):
                document['intended_purpose']='ticket'
                self.billing.store.update('document',owner,identifier,document)
                state['last_document_id']=identifier
                self._save_state(owner,state)
                await self.client.send(chat_id,'Ticket guardado. Completa tu perfil aquí.')
                await self.profiles.resume(owner,chat_id)
                return
        result=self.billing.classify_document(owner,identifier,purpose,profile_id=profile_id)
        current=self.billing.document(owner,result['id'])
        state['last_document_id']=current['id']
        if purpose=='csf':
            if state.get('draft_id'):
                try:
                    previous=self.billing.store.get('profile_draft',owner,state['draft_id'])
                except AppError as error:
                    if error.status!=404:
                        raise
                    previous=None
                if not previous or previous.get('document_id')!=current['id']:
                    state.update(draft_id=None,cfdi_use=None,profile_input=None)
            state['csf_document_id']=current['id']
            state['replace_profile_id']=None
            state['expected_upload']=None
        if current.get('ticket_id'):
            state['last_ticket_id']=current['ticket_id']
        self._save_state(owner,state)
        if purpose=='ticket' and current.get('extracted') is not None:
            await self._ticket(owner,chat_id,current['ticket_id'])
            return
        label='CSF' if purpose=='csf' else 'TICKET'
        profile_text=''
        if purpose=='ticket':
            profile=self.billing.store.get('profile',owner,profile_id)
            profile_text=' Perfil: '+profile['name']+'.'
        await self.client.send(chat_id,label+' recibido.'+profile_text+
            ' Analizarlo lo comparte con OpenAI y consume tokens. No se envía a comercios.',
            [('Autorizar análisis','an:'+current['id'])])

    async def _pending_tickets(self,owner,chat_id):
        pending=[item for item in self.billing.store.list('document',owner)
            if item.get('intended_purpose')=='ticket' and not item.get('superseded_by')]
        if pending:
            await self.client.send(chat_id,'Tickets guardados. Elige uno:',
                [('Continuar con ticket '+str(index+1),'dt:'+item['id']) for index,item in enumerate(pending[:10])])

    async def _documents(self,owner,chat_id):
        documents=[item for item in self.billing.store.list('document',owner)
            if item['purpose'] in {'csf','ticket','unclassified'} and not item.get('superseded_by')]
        if not documents:
            await self.client.send(chat_id,'No hay archivos de CSF o ticket disponibles. Los archivos temporales vencen según la política del servidor.')
            return
        labels={'csf':'CSF','ticket':'Ticket','unclassified':'Sin clasificar'}
        await self.client.send(chat_id,'Archivos propios recibidos. Elige uno para revisar o corregir su tipo:',
            [(labels[item.get('intended_purpose') or item['purpose']]+' · '+item['id'][:8],'dv:'+item['id']) for item in documents[:20]])

    async def _ticket(self,owner,chat_id,identifier):
        ticket=self.billing.store.get('ticket',owner,identifier)
        text=f"{ticket.get('merchant') or 'Ticket guardado'}\nFolio: {ticket.get('folio') or 'Pendiente'}\nImporte: {ticket.get('total') or 'Pendiente'}"
        buttons=[]
        try:
            document=self.billing.document(owner,ticket['document_id'])
        except AppError as error:
            if error.status!=404:
                raise
            document=None
            text+='\nEl archivo temporal venció; los datos siguen guardados.'
        if ticket['status']=='ERROR':
            text+='\n'+(ticket.get('analysis_error') or (document or {}).get('analysis_error') or {}).get('message',ticket.get('note') or 'El análisis anterior falló; no se determinó la legibilidad.')
        elif missing_receipt_fields(ticket):
            labels={'merchant':'comercio','purchase_date':'fecha','folio':'folio','total':'importe'}
            text+='\nFalta: '+', '.join(labels[key] for key in missing_receipt_fields(ticket))+'.'
            if document and (document.get('extracted') or {}).get('is_ticket'):
                buttons.extend(('Completar '+labels[key],'td:'+identifier+':'+key) for key in missing_receipt_fields(ticket))
        channels=self.billing.detect_billing_channels(owner,identifier)
        if channels['method'] in {'portal','portal_manual'}:
            if ticket.get('billing_url'):
                text+='\nPortal: '+ticket['billing_url']
            text+='\nPortal pendiente de automatizar.'
            buttons.append(('Ver datos para el portal','bp:'+identifier))
        elif channels['method']=='email':
            text+='\nFacturación: '+ticket['billing_email']
            if not ticket.get('verified_email'):
                text+='\nRevisa el destinatario. La solicitud incluirá ticket, datos y CSF si lo confirmas.'
                buttons.extend([('Confirmar correo + revisar con CSF','vc:'+identifier),('Revisé el correo (solo datos)','ve:'+identifier)])
            else:
                buttons.extend([('Preparar correo con datos y CSF','ec:'+identifier),('Preparar solo datos y ticket','pr:'+identifier)])
        elif ticket['status']!='ERROR':
            text+='\nNo se identificó página o correo de facturación.'
            if ticket.get('qr_urls'):
                text+='\nQR leído (confirma su finalidad en el ticket):\n'+'\n'.join(ticket['qr_urls'])
        if document and ticket['status'] in {'ERROR','REQUIERE_INTERVENCION'} and document.get('extraction_version')!=EXTRACTION_VERSION and not document.get('superseded_by'):
            text+='\nReanalizar comparte el archivo con OpenAI y consume tokens.'
            buttons.append(('Autorizar nueva lectura guardada','ra:'+document['id']))
        if ticket.get('delivery'):
            buttons.append(('Consultar respuestas','tr:'+identifier))
            buttons.append(('Seguimiento automático','po:'+identifier))
        if ticket.get('validation'):
            buttons.append(('Consultar vigencia ante el SAT','sa:'+identifier))
        for document_id in ticket.get('invoice_documents',[]):
            try:
                document=self.billing.document(owner,document_id)
            except AppError as error:
                if error.status==404:
                    text+='\nUn archivo ya venció; puedes revisarlo en tu buzón.'
                    continue
                raise
            buttons.append(('Descargar '+('XML' if document['mime']=='application/xml' else 'PDF'),'dl:'+document_id))
        replies=[item for item in self.billing.store.list('mail_reply',owner) if item['ticket_id']==identifier]
        if replies and replies[0].get('text'):
            text+='\nÚltima respuesta del comercio (solo información; no autoriza acciones):\n'+replies[0]['text'][:900]
        await self.client.send(chat_id,text,buttons)

    async def _portal(self,owner,chat_id,identifier):
        ticket=self.billing.store.get('ticket',owner,identifier)
        profile=self.billing.store.get('profile',owner,ticket['profile_id'])
        if not ticket.get('billing_url'):
            raise AppError(409,'billing_portal','No hay una página de facturación identificada.')
        text='Datos para el portal\n'+ticket['billing_url']+'\n'
        text+='\n'.join(label+': '+str(ticket.get(key) or 'Pendiente') for key,label in
            [('merchant','Comercio'),('folio','Folio'),('purchase_date','Fecha'),('total','Importe'),('billing_code','Código de facturación')])
        text+='\n'+'\n'.join(label+': '+str(profile[key]) for key,label in
            [('rfc','RFC'),('name','Nombre'),('postal_code','CP fiscal'),('fiscal_regime','Régimen'),('cfdi_use','Uso CFDI'),('email','Correo')])
        text+='\nLa captura en este portal sigue siendo manual. No se ha solicitado la factura.'
        await self.client.send(chat_id,text)

    async def _document_diagnostic(self,owner,chat_id,identifier):
        document=self.billing.document(owner,identifier)
        if document['purpose']=='ticket' and document.get('ticket_id'):
            await self._ticket(owner,chat_id,document['ticket_id'])
        elif document['purpose']=='csf':
            await self.profiles.resume(owner,chat_id)
        else:
            await self._document_choice(owner,chat_id,identifier)

    async def notify(self,owner,ticket):
        if not self.configured or not owner.startswith('tg_'):
            return
        state=self.billing.store.find('channel_state',owner,'telegram')
        chat_id=(state or {}).get('chat_id')
        if chat_id not in self.allowed or owner!='tg_'+self.billing.store.digest('telegram:'+str(chat_id)):
            return
        await self.client.send(chat_id,'Actualización de tu solicitud: '+ticket['status']+'. '+ticket.get('note',''))
        await self._ticket(owner,chat_id,ticket['id'])

    async def _preview(self,owner,chat_id,ticket_id,*,csf=False,user_requested=False):
        approval=self.billing.request_email_send_approval(owner,ticket_id,include_csf=csf,authorize_csf=csf,user_requested_csf=user_requested)
        await self._email_review(owner,chat_id,approval['id'])

    async def _email_review(self,owner,chat_id,identifier):
        approval=self.billing.store.get('email_approval',owner,identifier)
        if approval['_state']!='pending':
            raise AppError(409,'billing_approval','La autorización caducó o ya fue utilizada.')
        text=f"Revisa antes de enviar\nPara: {approval['to']}\nAsunto: {approval['subject']}\nAdjuntos: "+', '.join(item['label'] for item in approval['attachments'])+'\n\n'+approval['body']
        if approval.get('csf_share_reason')=='user_requested':
            text+='\nSe compartirá tu CSF completa por tu elección.'
        # Nunca ofrecer aprobar una revisión recortada por el límite de Telegram.
        if len(text)>3900:
            await self.client.send(chat_id,'El borrador es demasiado largo para revisarlo completo en Telegram. Recházalo y solicita una revisión manual.',
                [('Rechazar envío','no:'+approval['id'])])
        else:
            if not self.settings.billing_live_email_enabled or not self.billing.mail.ready(owner):
                text+='\nVista previa: falta activar el envío o conectar Gmail.'
                await self.client.send(chat_id,text,[('Rechazar borrador','no:'+approval['id'])])
            else:
                await self.client.send(chat_id,text,[('Confirmar y enviar este correo','se:'+approval['id']),('Rechazar envío','no:'+approval['id'])])

    async def show_review(self,owner,chat_id,review):
        identifier=review.get('id','')
        kind=review.get('kind')
        if kind=='profile_setup':
            await self.profiles.resume(owner,chat_id)
        elif kind=='email':
            await self._email_review(owner,chat_id,identifier)
        elif kind=='ticket':
            await self._ticket(owner,chat_id,identifier)
        elif kind=='document':
            document=self.billing.document(owner,identifier)
            if document['purpose']=='unclassified' or document.get('intended_purpose')=='ticket' or document.get('superseded_by'):
                await self._document_choice(owner,chat_id,identifier)
                return
            await self.client.send(chat_id,'Analizar este documento lo comparte con OpenAI. Revisa y autoriza solo si deseas continuar.',
                [('Autorizar análisis con OpenAI','an:'+identifier)])
        elif kind=='document_type':
            await self._document_choice(owner,chat_id,identifier)
        elif kind=='profile':
            await self.profiles.show_draft(owner,chat_id,identifier)

    async def handle(self,update):
        if not isinstance(update,dict):
            raise AppError(422,'telegram_update','Actualización de Telegram no válida.')
        callback=update.get('callback_query') or {}
        message=callback.get('message') or update.get('message') or {}
        sender=callback.get('from') or message.get('from') or {}
        chat=message.get('chat') or {}
        user_id=sender.get('id')
        if type(user_id) is not int or user_id not in self.allowed or chat.get('type')!='private' or chat.get('id')!=user_id:
            return {'ok':True,'ignored':True}
        owner='tg_'+self.billing.store.digest('telegram:'+str(user_id))
        update_id=update.get('update_id')
        if type(update_id) is not int:
            raise AppError(422,'telegram_update','Actualización de Telegram no válida.')
        lookup=self.billing.store.digest('telegram-update:'+str(update_id))
        if self.billing.store.find('channel_update',owner,lookup):
            return {'ok':True,'duplicate':True}
        lock=self._locks.setdefault(user_id,asyncio.Lock())
        if lock.locked():
            await self.client.send(user_id,'Estoy procesando tu solicitud anterior. Espera la respuesta y vuelve a enviar este mensaje.')
            return {'ok':True,'busy':True}
        async with lock:
            return await self._handle_authorized(owner,user_id,update_id,lookup,message,callback)

    async def _handle_authorized(self,owner,user_id,update_id,lookup,message,callback):
        today=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        with self.billing.database.connection() as conn:
            used=conn.execute("SELECT COUNT(*) FROM billing_records WHERE kind='channel_update' AND owner=? AND created>=?",(owner,today)).fetchone()[0]
        if used>=self.settings.billing_max_daily_telegram_updates:
            return {'ok':True,'rate_limited':True}
        # Persistir antes de cualquier efecto evita duplicados en reenvíos del webhook.
        self.billing.store.add('channel_update',owner,{},lookup=lookup,expires=time.time()+30*86400)
        state=self._state(owner)
        state['chat_id']=user_id
        self._save_state(owner,state)
        try:
            if callback:
                await self._callback(owner,user_id,callback.get('data',''))
                return {'ok':True}
            await self._message(owner,user_id,message)
        except AppError as error:
            await self.client.send(user_id,error.message)
        except Exception:
            await self.client.send(user_id,'No se completó la operación. Revisa el estado antes de repetir; no se reintentará automáticamente.')
        return {'ok':True}

    async def _callback(self,owner,chat_id,data):
        prefix,separator,identifier=data.partition(':')
        if not separator:
            raise AppError(422,'telegram_callback','Acción no válida.')
        code=None
        if prefix in {'cu','rf','td'}:
            identifier,_,code=identifier.partition(':')
        try:
            uuid.UUID(identifier)
        except ValueError:
            raise AppError(422,'telegram_callback','Acción no válida.') from None
        if prefix=='td':
            ticket=self.billing.store.get('ticket',owner,identifier)
            if code not in missing_receipt_fields(ticket):
                raise AppError(409,'ticket_field','Ese dato no está pendiente.')
            state=self._state(owner)
            state['ticket_input']={'id':identifier,'field':code}
            self._save_state(owner,state)
            examples={'merchant':'COMERCIO','purchase_date':'AAAA-MM-DD','folio':'FOLIO','total':'IMPORTE'}
            await self.client.send(chat_id,'Completa el dato: /dato_ticket '+examples[code])
        elif prefix in {'cu','rf','fc','pe','pm','pu','pp','px'}:
            await self.profiles.callback(owner,chat_id,prefix,identifier,code)
        elif prefix in {'dc','dt','dv'}:
            if prefix=='dv':
                await self._document_choice(owner,chat_id,identifier)
            else:
                await self._choose_document(owner,chat_id,identifier,'csf' if prefix=='dc' else 'ticket')
        elif prefix in {'go','gd'}:
            if not self.agent:
                raise AppError(503,'telegram_agent','El agente de Telegram no está configurado.')
            if prefix=='go':
                await self.agent.open(owner,chat_id,identifier)
            else:
                await self.agent.delete(owner,chat_id,identifier)
        elif prefix in {'ga','gr'}:
            if not self.agent:
                raise AppError(503,'telegram_agent','El agente de Telegram no está configurado.')
            await self.agent.decide(owner,chat_id,identifier,prefix=='ga')
        elif prefix=='an':
            document=self.billing.document(owner,identifier)
            if document['purpose']=='unclassified' or document.get('intended_purpose')=='ticket' or document.get('superseded_by'):
                await self._document_choice(owner,chat_id,identifier,reason='Primero confirma el tipo y el perfil del archivo; el botón anterior ya no corresponde a su estado actual.')
                return
            if document['purpose']=='csf':
                try:
                    draft=await self.billing.process_fiscal_certificate(owner,identifier,consent=True,replace_profile_id=document.get('profile_id'))
                except AppError as error:
                    if error.code=='not_fiscal_certificate':
                        state=self._state(owner)
                        if state.get('csf_document_id')==identifier:
                            state['csf_document_id']=None
                            self._save_state(owner,state)
                        await self._document_choice(owner,chat_id,identifier,reason='El análisis no identificó una CSF. Si es un ticket, corrige aquí el tipo; el archivo sigue guardado.')
                        return
                    raise
                await self.profiles.show_draft(owner,chat_id,draft['id'])
            else:
                ticket=await self.billing.analyze_ticket(owner,identifier,consent=True)
                state=self._state(owner)
                state['last_ticket_id']=ticket['id']
                self._save_state(owner,state)
                await self._ticket(owner,chat_id,ticket['id'])
        elif prefix=='ra':
            document=self.billing.retry_ticket_after_update(owner,identifier)
            state=self._state(owner)
            state.update(last_document_id=document['id'],last_ticket_id=self.billing.document(owner,document['id'])['ticket_id'])
            self._save_state(owner,state)
            ticket=await self.billing.analyze_ticket(owner,document['id'],consent=True)
            await self._ticket(owner,chat_id,ticket['id'])
        elif prefix=='bp':
            await self._portal(owner,chat_id,identifier)
        elif prefix=='pf':
            await self.profiles.confirm(owner,chat_id,identifier)
        elif prefix=='pd':
            self.profiles.current_draft(owner,identifier)
            self.billing.store.delete('profile_draft',owner,identifier)
            state=self._state(owner)
            state.update(draft_id=None,cfdi_use=None,profile_input=None,expected_upload=None,replace_profile_id=None)
            self._save_state(owner,state)
            await self.client.send(chat_id,'Cambios descartados.')
        elif prefix=='ve':
            self.billing.verify_billing_email(owner,identifier,confirmed=True)
            await self._ticket(owner,chat_id,identifier)
        elif prefix in {'vc','ec'}:
            if prefix=='vc':
                self.billing.verify_billing_email(owner,identifier,confirmed=True)
            await self._preview(owner,chat_id,identifier,csf=True,user_requested=True)
        elif prefix=='pr':
            await self._preview(owner,chat_id,identifier)
        elif prefix=='se':
            ticket=await self.billing.send_invoice_request_email(owner,identifier,confirmed=True)
            await self.client.send(chat_id,'Solicitud enviada. Estado: '+ticket['status']+'. El envío no significa que la factura esté emitida.')
        elif prefix=='no':
            self.billing.reject_email(owner,identifier)
            await self.client.send(chat_id,'Envío rechazado; no se envió el correo.')
        elif prefix=='tr':
            await self.billing.track_invoice_email(owner,identifier,authorize_sat=False)
            await self._ticket(owner,chat_id,identifier)
        elif prefix=='sa':
            self.billing.store.get('ticket',owner,identifier)
            await self.client.send(chat_id,'Esta consulta comparte RFC del emisor y receptor, UUID e importe con el SAT; no envía la constancia. ¿Autorizas esta consulta?', [('Autorizar consulta al SAT','sv:'+identifier)])
        elif prefix=='sv':
            await self.billing.validate_cfdi(owner,identifier,authorize_sat=True)
            await self._ticket(owner,chat_id,identifier)
        elif prefix=='dl':
            document=self.billing.document(owner,identifier)
            if document['purpose'] not in {'invoice_xml','invoice_pdf'}:
                raise AppError(403,'telegram_document','Este botón solo entrega archivos de factura propios.')
            await self.client.send_document(chat_id,document)
        elif prefix=='po':
            self.billing.store.get('ticket',owner,identifier)
            minutes=self.settings.billing_tracking_interval_seconds//60
            await self.client.send(chat_id,f'Con tu autorización se consultará Gmail cada {minutes} minutos, hasta {self.settings.billing_tracking_max_checks} consultas para este ticket, deteniéndose tras tres errores. No se enviarán recordatorios ni se compartirá nada con el SAT. Puedes detenerlo cuando quieras.',
                [('Autorizar seguimiento','on:'+identifier),('Detener seguimiento','of:'+identifier)])
        elif prefix in {'on','of'}:
            self.billing.configure_tracking(owner,identifier,enabled=prefix=='on',confirmed=True)
            await self.client.send(chat_id,'Seguimiento '+('activado con la política revisada.' if prefix=='on' else 'detenido.'))

    async def _message(self,owner,chat_id,message):
        text=message.get('text','').strip()
        declaration=text.casefold().strip(' .!¡?¿')
        state=self._state(owner)
        profiles=self.billing.store.list('profile',owner)
        if text in {'/start','/ayuda'}:
            draft,csf=self._profile_setup(owner,state) if not profiles else (None,None)
            status='Perfil listo. Envía tu ticket.' if profiles else ('Completa tu perfil aquí: /perfil.' if draft or csf else 'Envía tu CSF una vez. Después, tus tickets.')
            await self.client.send(chat_id,status)
            if text=='/ayuda':
                await self.client.send(chat_id,'/perfil · /tickets · /documentos\n/nuevo · /historial\nLa IA consume tokens.')
        elif text=='/nuevo' and self.agent:
            self.agent.conversation(owner,new=True)
            await self.client.send(chat_id,'Chat nuevo. Tu perfil y tickets se conservan.')
        elif text=='/historial' and self.agent:
            await self.agent.history(owner,chat_id)
        elif text=='/conversaciones' and self.agent:
            await self.agent.conversations(owner,chat_id)
        elif text=='/documentos':
            await self._documents(owner,chat_id)
        elif declaration in {'es un ticket','es ticket','es un recibo','/ticket','es una csf','es csf','es una constancia','/csf'}:
            identifier=state.get('last_document_id')
            if not identifier:
                await self.client.send(chat_id,'Elige el archivo recibido con /documentos para indicar su tipo.')
            else:
                await self._choose_document(owner,chat_id,identifier,'ticket' if declaration in {'es un ticket','es ticket','es un recibo','/ticket'} else 'csf')
        elif text.startswith('/eliminar_conversacion ') and self.agent:
            identifier=text.split(maxsplit=1)[1]
            self.agent.service.public_conversation(identifier,owner)
            await self.client.send(chat_id,'¿Confirmas eliminar los mensajes y aprobaciones pendientes de esta conversación? Tus perfiles y tickets se conservan.',
                [('Confirmar eliminación','gd:'+identifier)])
        elif text.startswith('/vincular_'):
            # Código de configuración previo al webhook; nunca autoriza cuentas aquí.
            return
        elif text=='/usos':
            draft,_=self._profile_setup(owner,state)
            if draft:
                if not draft.get('cfdi_use'):
                    await self.profiles.show_draft(owner,chat_id,draft['id'])
                else:
                    await self.profiles.uses(owner,chat_id,draft['id'])
            else:
                await self.client.send(chat_id,'Elige el uso correcto según tu situación fiscal:\n'+'\n'.join(key+' — '+value for key,value in CFDI_USES.items()))
        elif text=='/perfiles':
            if profiles:
                await self.client.send(chat_id,'Elige tu perfil.',[(item['name'][:55],'pp:'+item['id']) for item in profiles])
            else:
                await self.profiles.resume(owner,chat_id)
        elif text.startswith('/elegir '):
            identifier=text.split(maxsplit=1)[1]
            self.billing.store.get('profile',owner,identifier)
            state['active_profile']=identifier
            self._save_state(owner,state)
            await self.client.send(chat_id,'Perfil seleccionado para los siguientes tickets.')
            await self._pending_tickets(owner,chat_id)
        elif text.startswith('/eliminar_perfil '):
            identifier=text.split(maxsplit=1)[1]
            profile=self.billing.store.get('profile',owner,identifier)
            await self.client.send(chat_id,'Para confirmar que quieres eliminar este perfil, sus solicitudes y archivos, envía:\n/confirmar_eliminacion '+profile['id'])
        elif text.startswith('/confirmar_eliminacion '):
            self.billing.delete_profile(owner,text.split(maxsplit=1)[1])
            await self.client.send(chat_id,'Perfil y archivos asociados eliminados.')
        elif text=='/tickets':
            tickets=[item for item in self.billing.store.list('ticket',owner) if not item.get('superseded_by')]
            for ticket in tickets[:10]:
                await self._ticket(owner,chat_id,ticket['id'])
            if not tickets:
                await self.client.send(chat_id,'No hay solicitudes; primero confirma un perfil y envía un ticket.')
        elif text=='/conectar_gmail':
            url=self.billing.mail.connect(owner)
            await self.client.send(chat_id,'Autoriza en Google el envío y la lectura de este buzón. No compartas contraseñas ni tokens aquí. Después vuelve a Telegram.\n'+url)
        elif text.startswith('/requiere_csf '):
            parts=text.split(maxsplit=2)
            if len(parts)!=3:
                raise AppError(422,'csf_request','Usa /requiere_csf ID_TICKET descripción de la petición expresa del comercio.')
            self.billing.mark_certificate_required(owner,parts[1],confirmed=True,evidence=parts[2])
            await self.client.send(chat_id,'Petición de constancia registrada. Solo se adjuntará si autorizas /preparar_con_csf ID_TICKET y luego confirmas el correo completo.')
        elif text.startswith('/preparar_con_csf '):
            await self._preview(owner,chat_id,text.split(maxsplit=1)[1],csf=True)
        elif message.get('document') or message.get('photo'):
            caption=message.get('caption','').strip()
            attachment=message.get('document') or message['photo'][-1]
            if attachment.get('file_size',0)>self.settings.billing_max_upload_bytes:
                raise AppError(413,'document_size','El archivo supera el límite permitido.')
            data=await self.client.download(attachment['file_id'])
            document=self.billing.receive_document(owner,'unclassified',data)
            state['last_document_id']=document['id']
            self._save_state(owner,state)
            if caption.casefold()=='/ticket':
                await self._choose_document(owner,chat_id,document['id'],'ticket')
            elif caption.casefold()=='/csf' or caption.casefold().startswith('/csf '):
                await self._choose_document(owner,chat_id,document['id'],'csf',profile_id=caption.split(maxsplit=1)[1] if ' ' in caption else state.get('replace_profile_id'))
            elif state.get('replace_profile_id'):
                await self._choose_document(owner,chat_id,document['id'],'csf',profile_id=state['replace_profile_id'])
            elif state.get('expected_upload')=='csf':
                await self._choose_document(owner,chat_id,document['id'],'csf')
            elif not profiles:
                draft,csf=self._profile_setup(owner,state)
                filename=attachment.get('file_name','').casefold()
                named_csf=filename.startswith(('csf','constancia')) or 'situacion_fiscal' in filename or 'situación_fiscal' in filename
                retry_csf=(draft and not validate_fiscal(draft)) or (csf and csf.get('extracted') is not None and not csf['extracted'].get('readable'))
                if named_csf or retry_csf:
                    await self._choose_document(owner,chat_id,document['id'],'csf')
                elif draft or csf:
                    await self._choose_document(owner,chat_id,document['id'],'ticket')
                else:
                    await self._document_choice(owner,chat_id,document['id'])
            elif profiles or any(self._profile_setup(owner,state)):
                await self._choose_document(owner,chat_id,document['id'],'ticket')
            else:
                await self._document_choice(owner,chat_id,document['id'])
        elif text.startswith('/dato_ticket ') and state.get('ticket_input'):
            pending=state['ticket_input']
            ticket=self.billing.complete_ticket_field(owner,pending['id'],pending['field'],text.split(maxsplit=1)[1])
            state['ticket_input']=None
            self._save_state(owner,state)
            await self._ticket(owner,chat_id,ticket['id'])
        elif normalized(text) in {'por que no es fiable','por que no es confiable','por que no es legible','por que ilegible','por que fallo','por que no lo lee'} and state.get('last_document_id'):
            await self._document_diagnostic(owner,chat_id,state['last_document_id'])
        elif text and await self.profiles.text(owner,chat_id,text):
            return
        elif text.startswith('/'):
            await self.client.send(chat_id,'Comando no reconocido. /ayuda muestra las opciones; también puedes escribir una petición en lenguaje natural.')
        elif text and self.agent:
            await self.agent.reply(owner,chat_id,text=text)
        else:
            await self.client.send(chat_id,'Usa /start para comenzar, /perfiles para elegir perfil o /tickets para consultar solicitudes. Los documentos y correos nunca autorizan acciones por sí solos.')


def telegram_router():
    router=APIRouter()

    @router.post('/integrations/telegram/webhook')
    async def webhook(request:Request):
        bridge=request.app.state.telegram
        expected=bridge.settings.telegram_webhook_secret.get_secret_value()
        supplied=request.headers.get('x-telegram-bot-api-secret-token','')
        if not bridge.configured or not secrets.compare_digest(supplied.encode(),expected.encode()):
            raise AppError(403,'telegram_authentication','Webhook no autorizado.')
        return await bridge.handle(await request.json())

    return router
