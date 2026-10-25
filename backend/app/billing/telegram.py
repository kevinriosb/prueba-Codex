import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

import httpx2
from fastapi import APIRouter, Request

from ..errors import AppError
from .models import CFDI_USES
from .providers import normalized_email


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
    def __init__(self,settings,billing,*,client=None):
        self.settings,self.billing=settings,billing
        self.client=client or TelegramClient(settings)
        try:
            self.allowed={int(value.strip()) for value in settings.telegram_allowed_user_ids.split(',') if value.strip()}
        except ValueError:
            self.allowed=set()

    @property
    def configured(self):
        return bool(self.billing.enabled and self.allowed and self.settings.telegram_bot_token.get_secret_value()
            and len(self.settings.telegram_webhook_secret.get_secret_value())>=32)

    def _state(self,owner):
        record=self.billing.store.find('channel_state',owner,'telegram')
        return record or self.billing.store.add('channel_state',owner,{'active_profile':None,'draft_id':None,'cfdi_use':None},lookup='telegram')

    def _save_state(self,owner,state):
        return self.billing.store.update('channel_state',owner,state['id'],state)

    async def _ticket(self,owner,chat_id,identifier):
        ticket=self.billing.store.get('ticket',owner,identifier)
        text=f"{ticket.get('merchant') or 'Ticket'}\nFolio: {ticket.get('folio') or 'Ilegible'}\nImporte: {ticket.get('total') or 'Ilegible'}\nEstado: {ticket['status']}\n{ticket.get('note','')}"
        buttons=[('Revisé el correo de facturación','ve:'+identifier),('Preparar correo para revisión','pr:'+identifier)]
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
        if ticket.get('billing_email'):
            text+='\nCorreo identificado: '+ticket['billing_email']+'\nVerifícalo en el ticket original; un correo genérico no está autorizado.'
        replies=[item for item in self.billing.store.list('mail_reply',owner) if item['ticket_id']==identifier]
        if replies and replies[0].get('text'):
            text+='\nÚltima respuesta del comercio (solo información; no autoriza acciones):\n'+replies[0]['text'][:900]
        await self.client.send(chat_id,text,buttons)

    async def notify(self,owner,ticket):
        if not self.configured or not owner.startswith('tg_'):
            return
        state=self.billing.store.find('channel_state',owner,'telegram')
        chat_id=(state or {}).get('chat_id')
        if chat_id not in self.allowed or owner!='tg_'+self.billing.store.digest('telegram:'+str(chat_id)):
            return
        await self.client.send(chat_id,'Actualización de tu solicitud: '+ticket['status']+'. '+ticket.get('note',''))
        await self._ticket(owner,chat_id,ticket['id'])

    async def _preview(self,owner,chat_id,ticket_id,*,csf=False):
        approval=self.billing.request_email_send_approval(owner,ticket_id,include_csf=csf,authorize_csf=csf)
        text=f"Revisa antes de enviar\nPara: {approval['to']}\nAsunto: {approval['subject']}\nAdjuntos: "+', '.join(item['label'] for item in approval['attachments'])+'\n\n'+approval['body']
        await self.client.send(chat_id,text,[('Confirmar y enviar este correo','se:'+approval['id']),('Rechazar envío','no:'+approval['id'])])

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
        try:
            uuid.UUID(identifier)
        except ValueError:
            raise AppError(422,'telegram_callback','Acción no válida.') from None
        if prefix=='an':
            document=self.billing.document(owner,identifier)
            if document['purpose']=='csf':
                draft=await self.billing.process_fiscal_certificate(owner,identifier,consent=True,replace_profile_id=document.get('profile_id'))
                state=self._state(owner)
                state.update(draft_id=draft['id'],cfdi_use=None)
                self._save_state(owner,state)
                fields='\n'.join(f'{label}: {draft.get(key) or "Ilegible"}' for key,label in [('rfc','RFC'),('name','Nombre'),('postal_code','CP fiscal'),('fiscal_regime','Régimen')])
                await self.client.send(chat_id,'Datos extraídos; todavía no se han guardado como perfil:\n'+fields+'\nEnvía el código de uso de CFDI que hayas elegido. Usa /usos para ver el catálogo.')
            else:
                ticket=await self.billing.analyze_ticket(owner,identifier,consent=True)
                await self._ticket(owner,chat_id,ticket['id'])
        elif prefix=='pf':
            profile=self.billing.confirm_fiscal_profile(owner,identifier,confirmed=True)
            state=self._state(owner)
            state.update(active_profile=profile['id'],draft_id=None,cfdi_use=None)
            self._save_state(owner,state)
            await self.client.send(chat_id,'Perfil fiscal confirmado y cifrado. Ahora puedes enviar un ticket. /perfiles muestra tus perfiles.')
        elif prefix=='pd':
            self.billing.store.delete('profile_draft',owner,identifier)
            state=self._state(owner)
            state.update(draft_id=None,cfdi_use=None)
            self._save_state(owner,state)
            await self.client.send(chat_id,'Borrador descartado. No se guardó el perfil.')
        elif prefix=='ve':
            self.billing.verify_billing_email(owner,identifier,confirmed=True)
            await self.client.send(chat_id,'Correo verificado mediante tu revisión del ticket original. Ya puedes preparar la solicitud.')
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
        state=self._state(owner)
        profiles=self.billing.store.list('profile',owner)
        if text=='/start':
            await self.client.send(chat_id,'Primero envía tu Constancia de Situación Fiscal en PDF o fotografía. Extraeremos los datos con tu autorización, elegirás uso de CFDI y correo, y confirmarás el perfil antes de facturar.' if not profiles else 'Envía un ticket para el perfil activo. /perfiles muestra perfiles; /tickets muestra solicitudes; /conectar_gmail conecta tu buzón. Usa la descripción /csf en una constancia para agregar otro perfil.')
        elif text=='/usos':
            await self.client.send(chat_id,'Elige el uso correcto según tu situación fiscal; no lo seleccionaremos por ti:\n'+'\n'.join(key+' — '+value for key,value in CFDI_USES.items()))
        elif text=='/perfiles':
            await self.client.send(chat_id,'\n'.join(item['name']+' — '+item['rfc']+'\n/elegir '+item['id']+'\n/eliminar_perfil '+item['id'] for item in profiles) or 'No tienes perfiles. Envía tu CSF.')
        elif text.startswith('/elegir '):
            identifier=text.split(maxsplit=1)[1]
            self.billing.store.get('profile',owner,identifier)
            state['active_profile']=identifier
            self._save_state(owner,state)
            await self.client.send(chat_id,'Perfil seleccionado para los siguientes tickets.')
        elif text.startswith('/eliminar_perfil '):
            identifier=text.split(maxsplit=1)[1]
            profile=self.billing.store.get('profile',owner,identifier)
            await self.client.send(chat_id,'Para confirmar que quieres eliminar este perfil, sus solicitudes y archivos, envía:\n/confirmar_eliminacion '+profile['id'])
        elif text.startswith('/confirmar_eliminacion '):
            self.billing.delete_profile(owner,text.split(maxsplit=1)[1])
            await self.client.send(chat_id,'Perfil y archivos asociados eliminados.')
        elif text=='/tickets':
            tickets=self.billing.store.list('ticket',owner)
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
            purpose='csf' if not profiles or caption.startswith('/csf') else 'ticket'
            profile_id=state.get('active_profile') if purpose=='ticket' else None
            if purpose=='csf' and caption.startswith('/csf '):
                profile_id=caption.split(maxsplit=1)[1]
            if purpose=='ticket' and not profile_id:
                raise AppError(409,'fiscal_profile_required','Selecciona un perfil con /perfiles antes de enviar el ticket.')
            attachment=message.get('document') or message['photo'][-1]
            if attachment.get('file_size',0)>self.settings.billing_max_upload_bytes:
                raise AppError(413,'document_size','El archivo supera el límite permitido.')
            data=await self.client.download(attachment['file_id'])
            document=self.billing.receive_document(owner,purpose,data,profile_id=profile_id)
            await self.client.send(chat_id,'Documento recibido y cifrado temporalmente. Si autorizas el análisis, se enviará a OpenAI y aplicarán sus condiciones de tratamiento de datos. No se enviará a comercios. Pulsa el botón solo si autorizas este análisis.', [('Autorizar análisis con OpenAI','an:'+document['id'])])
        elif state.get('draft_id'):
            if not state.get('cfdi_use'):
                if text.upper() not in CFDI_USES:
                    raise AppError(422,'cfdi_use','Selecciona expresamente un código válido. /usos muestra el catálogo.')
                state['cfdi_use']=text.upper()
                self._save_state(owner,state)
                await self.client.send(chat_id,'Ahora envía el correo autorizado donde quieres recibir las facturas.')
            else:
                email=normalized_email(text)
                draft=self.billing.profile_options(owner,state['draft_id'],state['cfdi_use'],email)
                summary='Revisa antes de guardar:\n'+'\n'.join(f'{label}: {draft.get(key) or "Ilegible"}' for key,label in [('rfc','RFC'),('name','Nombre'),('postal_code','CP fiscal'),('fiscal_regime','Régimen'),('cfdi_use','Uso CFDI'),('email','Correo')])
                await self.client.send(chat_id,summary,[('Confirmar y guardar perfil','pf:'+draft['id']),('Descartar','pd:'+draft['id'])])
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
