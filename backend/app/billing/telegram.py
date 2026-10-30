import asyncio
import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx2
from fastapi import APIRouter, Request

from ...browser_worker.policy import RESUMABLE_LIMITS
from ..errors import AppError
from .models import CFDI_USES, missing_fiscal_fields, validate_fiscal
from .extraction import EXTRACTION_VERSION
from .receipt import browser_missing_fields, missing_receipt_fields, pending_receipt_fields
from .telegram_agent import TelegramAgent
from .telegram_profiles import TelegramProfiles, normalized
from .telegram_requests import REQUEST_TEXTS, TelegramRequests
from .telegram_workflow import TelegramWorkflow
from .telegram_submission import TelegramSubmission
from .telegram_discovery import TelegramDiscovery
from .telegram_receipts import TelegramReceipts
from .portal_permissions import PortalPermissionRequired,portal_origin
from .portal_memory import matching_brand,brand_options,has_billing_form,public_form_ready,unresolved_brand_selector


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
        self.requests=TelegramRequests(self)
        self.workflow=TelegramWorkflow(self)
        self.submission=TelegramSubmission(self)
        self.discovery=TelegramDiscovery(self)
        self.receipts=TelegramReceipts(self)
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
        if draft and draft.get('document_id') in confirmed_documents and not draft.get('replace_profile_id'):
            # Un botón/modelo antiguo no convierte una CSF confirmada en un alta.
            draft=None
            state.update(draft_id=None,cfdi_use=None,profile_input=None,expected_upload=None,
                csf_document_id=None,replace_profile_id=None)
            self._save_state(owner,state)
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
        active=self.workflow.active(owner)
        state=self._state(owner)
        profiles=self.billing.store.list('profile',owner)
        documents=[item for item in self.billing.store.list('document',owner)
            if item['purpose'] in {'csf','ticket','unclassified'} and not item.get('superseded_by')]
        last=next((item for item in documents if item['id']==state.get('last_document_id')),None)
        last_ticket=None
        if last and last.get('ticket_id'):
            try:
                last_ticket=self.billing.resolve_ticket(owner,last['ticket_id'])
            except AppError as error:
                if error.status!=404:
                    raise
        draft,csf=self._profile_setup(owner,state,documents)
        return {'profile_confirmed':bool(profiles),'active_profile_id':state.get('active_profile') if any(item['id']==state.get('active_profile') for item in profiles) else None,
            'active_ticket_id':active['id'] if active else None,
            'workflow_mode':state.get('workflow_mode'),
            'browser_ticket_id':state.get('browser_ticket_id') if active and state.get('browser_ticket_id')==active['id'] else None,
            'browser_available':self.billing.browser.enabled,
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
            [('Es una CSF','dc:'+identifier),('Es un ticket','dt:'+identifier)]+self.workflow.navigation(owner))

    async def _choose_document(self,owner,chat_id,identifier,purpose,*,profile_id=None,workflow_confirmed=False):
        document=self.billing.document(owner,identifier)
        if document['purpose'] not in {'csf','ticket','unclassified'}:
            raise AppError(422,'document_purpose','No se puede cambiar el tipo de este archivo.')
        state=self._state(owner)
        if purpose=='csf' and not profile_id and not state.get('replace_profile_id'):
            confirmed=next((item for item in self.billing.store.list('profile',owner) if item.get('document_id')==identifier),None)
            if confirmed:
                state['active_profile']=confirmed['id']
                self._save_state(owner,state)
                await self.profiles.resume(owner,chat_id)
                return
        if purpose=='ticket':
            if not workflow_confirmed and not await self.workflow.admit(owner,chat_id,identifier):
                return
            state=self._state(owner)
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
        if purpose=='ticket' and current.get('ticket_id'):
            self.workflow.activate(owner,self.billing.resolve_ticket(owner,current['ticket_id']))
        if purpose=='ticket' and current.get('extracted') is not None:
            await self._ticket(owner,chat_id,current['ticket_id'])
            return
        label='CSF' if purpose=='csf' else 'TICKET'
        profile_text=''
        if purpose=='ticket':
            profile=self.billing.store.get('profile',owner,profile_id)
            profile_text=' Perfil: '+profile['name']+'.'
            if hasattr(self.billing.extractor,'extract_routing'):
                modern=getattr(self.billing.extractor,'routing_version',2)>=3
                current['analysis_scope_offered']='portal-fields-v3' if modern else 'portal-fields-v2'
                self.billing.store.update('document',owner,current['id'],current)
                text=('Analizaré el ticket y buscaré facturación en la web oficial con IA. Usa saldo de OpenAI; confirmarás el envío.' if modern else
                    'Leeré este ticket con OpenAI: portal y campos necesarios. Cada lectura usa tokens; no solicita factura.')
                await self.client.send(chat_id,'Solicitud '+current['ticket_id'][:8]+'\n'+text,
                    [('Autorizar análisis','an:'+current['id'])]+self.workflow.navigation(owner))
                return
        await self.client.send(chat_id,label+' recibido.'+profile_text+
            ' Analizarlo lo comparte con OpenAI y consume tokens. No se envía a comercios.',
            [('Autorizar análisis','an:'+current['id'])]+self.workflow.navigation(owner))

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
        ticket=self.billing.resolve_ticket(owner,identifier)
        ticket=self.billing.portal_discovery.repair(owner,ticket)
        identifier=ticket['id']
        active=self.workflow.active(owner)
        state=self._state(owner)
        if not active or active['id']==identifier:
            state['last_ticket_id']=identifier
        self._save_state(owner,state)
        text='Solicitud '+identifier[:8]+'\n'+(ticket.get('merchant') or 'Ticket guardado')+'\n'+self.workflow.stage(ticket)
        if ticket.get('telegram_workflow') in {'paused','cancelled'}:
            label='Pausada' if ticket['telegram_workflow']=='paused' else 'Cancelada en el bot'
            summary,_,_=self.requests.summary(owner,ticket)
            downloads=[('Descargar factura','dl:'+document_id) for document_id in ticket.get('invoice_documents',[])]
            await self.client.send(chat_id,text+'\n'+label+'.\n'+summary+'\nLo ya enviado al comercio no se anula.',
                [('Retomar solicitud','ts:'+identifier)]+downloads+self.workflow.navigation(owner))
            return
        transaction=self.billing.portal_submission.current(owner,identifier)
        if transaction:
            await self.submission.show(owner,chat_id,identifier,tx=transaction)
            return
        for key,label in [('account_number','Cuenta'),('folio','Folio'),('store_number','Tienda'),('reference_number','Referencia'),('billing_code','Código de facturación')]:
            if ticket.get(key):
                text+='\n'+label+': '+ticket[key]
        if ticket.get('total'):text+='\nImporte: '+str(ticket['total'])
        try:
            document=self.billing.document(owner,ticket['document_id'])
        except AppError as error:
            if error.status!=404:
                raise
            document=None
            text+='\nEl archivo temporal venció; los datos siguen guardados.'
        analysis=self.requests.analysis_status(owner,ticket,document)
        summary,approval_id,can_prepare=self.requests.summary(owner,ticket,analysis=analysis)
        text+='\n'+summary
        buttons=[]
        if approval_id:
            buttons.append(('Revisar solicitud pendiente','er:'+approval_id))
        if document and not document.get('analysis_consent') and document.get('extracted') is None and (analysis is None or analysis['available']):
            text+='\nAnalizar comparte el archivo con OpenAI y consume tokens.'
            buttons.append(('Autorizar análisis','an:'+document['id']))
        if ticket['status']=='ERROR':
            text+='\n'+(analysis['message'] if analysis else
                (ticket.get('analysis_error') or (document or {}).get('analysis_error') or {}).get('message',ticket.get('note') or 'El análisis anterior falló; no se determinó la legibilidad.'))
        elif ticket.get('portal_stage')=='awaiting_form':
            text+='\n'+('Revisaré los campos adicionales del portal.' if ticket.get('receipt_summary_read') else 'Los datos de compra se leerán después del formulario.')
            if ticket.get('receipt_summary_read'):
                missing=[key for key in ('total','purchase_date','merchant') if not ticket.get(key)]
                names={'total':'importe','purchase_date':'fecha','merchant':'comercio'}
                if missing:
                    text+='\nNo leí: '+', '.join(names[key] for key in missing)+'. Escríbelo o envía foto de ese campo.'
                    buttons.extend(('Completar '+names[key],'td:'+identifier+':'+key) for key in missing)
        elif pending_receipt_fields(ticket):
            labels={'merchant':'comercio','purchase_date':'fecha','folio':'folio','total':'importe',
                'account_number':'Cuenta','reference_number':'referencia','billing_code':'código de facturación',
                'web_id':'Web ID','store_number':'Tienda','register_number':'Caja','toll_station':'caseta','lane':'carril','purchase_time':'hora'}
            text+='\nNo leí: '+', '.join(labels[key] for key in pending_receipt_fields(ticket))+'. Escríbelo o envía foto de ese campo.'
            if ticket.get('is_ticket') is True or document and (document.get('extracted') or {}).get('is_ticket'):
                buttons.extend(('Completar '+labels[key],'td:'+identifier+':'+key) for key in pending_receipt_fields(ticket))
        channels=self.billing.detect_billing_channels(owner,identifier)
        if channels['method'] in {'portal','portal_manual'}:
            if ticket.get('portal_discovery_error'):text+='\n'+ticket['portal_discovery_error']
            if ticket.get('billing_url'):
                entry=self.billing.portal_memory.entry_url(owner,ticket)
                text+='\n'+('Portal guardado: ' if entry!=ticket['billing_url'] else 'Portal: ')+entry
                if ticket.get('selected_portal'):
                    buttons.append(('Usar enlace del ticket','bo:'+identifier))
                elif self.billing.portal_memory.suggestion(owner,ticket):
                    buttons.append(('Usar portal guardado','bm:'+identifier))
            plan=ticket.get('portal_inspection') or {}
            if plan.get('fields'):
                prefix='Portal pide: ' if plan.get('status')=='verified' else 'Según plantilla, sin verificar en línea: '
                text+='\n'+prefix+', '.join(item['label'] for item in plan['fields'])
            text+='\n'+('Revisión pública disponible; solicitud aún manual.' if self.billing.browser.enabled else 'Portal pendiente de automatizar.')
            buttons.append(('Ver datos para el portal','bp:'+identifier))
            if self.billing.browser.enabled:
                if hasattr(self.billing.browser,'transaction_view') and (ticket.get('portal_browser') or {}).get('fields'):
                    buttons.append(('Continuar facturación','lv:'+identifier))
                if not self.billing.portal_permissions.route_allowed(owner,ticket):
                    permission=self.billing.portal_permissions.request(owner,identifier,route=True)
                    text+='\n¿Permites revisar este portal y seguir su ruta pública de facturación, confirmando cada nuevo dominio? Usa IA; sin enviar datos fiscales ni facturar.'
                    buttons.extend(self._permission_buttons(permission))
                else:
                    buttons.append(('Revisar portal con IA','bn:'+identifier))
        elif channels['method']=='email':
            text+='\nFacturación: '+ticket['billing_email']
            if not can_prepare:
                pass
            elif not ticket.get('verified_email'):
                text+='\nRevisa el destinatario. La solicitud incluirá ticket, datos y CSF si lo confirmas.'
                buttons.extend([('Confirmar correo + revisar con CSF','vc:'+identifier),('Revisé el correo (solo datos)','ve:'+identifier)])
            else:
                buttons.extend([('Preparar correo con datos y CSF','ec:'+identifier),('Preparar solo datos y ticket','pr:'+identifier)])
        elif ticket['status'] not in {'ERROR','TICKET_RECIBIDO'}:
            text+='\n'+(ticket.get('portal_discovery_error') or 'Buscaré Facturación en la web oficial del comercio.')
            text+='\nBuscar con IA usa saldo de OpenAI.'
            buttons.extend([('Buscar facturación oficial con IA','df:'+identifier),('Escribir web oficial','dw:'+identifier)])
        old_failure=ticket['status']=='ERROR' and (ticket.get('analysis_error') or {}).get('code') not in {'billing_daily_limit','billing_busy'}
        if document and (document.get('analysis_consent') or old_failure) and (ticket['status'] in {'ERROR','REQUIERE_INTERVENCION'} or pending_receipt_fields(ticket)) and document.get('extraction_version')!=EXTRACTION_VERSION and not document.get('superseded_by') and not ticket.get('delivery'):
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
        buttons.append(('Actualizar estado','ts:'+identifier))
        replies=[item for item in self.billing.store.list('mail_reply',owner) if item['ticket_id']==identifier]
        if replies and replies[0].get('text'):
            text+='\nÚltima respuesta del comercio (solo información; no autoriza acciones):\n'+replies[0]['text'][:900]
        await self.client.send(chat_id,text,buttons+self.workflow.navigation(owner))

    async def _portal(self,owner,chat_id,identifier):
        current=self.billing.resolve_ticket(owner,identifier)
        try:
            self.billing.portal_permissions.require(owner,current)
        except PortalPermissionRequired:
            permission=self.billing.portal_permissions.request(owner,current['id'],intent='details')
            await self._portal_permission(owner,chat_id,permission['id'])
            return
        if current.get('portal_browser'):
            await self._browser(owner,chat_id,current['id'])
            return
        ticket=await self.billing.inspect_billing_portal(owner,identifier)
        profile=self.billing.store.get('profile',owner,ticket['profile_id'])
        if not ticket.get('billing_url'):
            raise AppError(409,'billing_portal','No hay una página de facturación identificada.')
        text='Datos para el portal\n'+self.billing.portal_memory.entry_url(owner,ticket)+'\n'
        plan=ticket['portal_inspection']
        if plan['status']=='verified':
            text+='Campos leídos del formulario público:\n'
        else:
            text+='No se verificó el formulario en línea.\n'
        for item in plan['fields']:
            text+=item['label']+': '+str(item['value'] or 'Pendiente')+'\n'
        if plan.get('alternatives'):
            text+='Otras referencias del ticket (sin validar en el portal):\n'
            text+='\n'.join(item['label']+': '+item['value'] for item in plan['alternatives'])+'\n'
        if not plan['fields'] and plan.get('proposed_reference'):
            item=plan['proposed_reference']
            text+='Referencia propuesta, sin validar: '+item['label']+' '+item['value']+'\n'
        text+='\n'.join(label+': '+str(ticket[key]) for key,label in
            [('merchant','Comercio'),('account_number','Cuenta'),('folio','Folio'),('reference_number','Referencia'),('purchase_date','Fecha'),('total','Importe'),('billing_code','Código de facturación')] if ticket.get(key))
        text+='\n'+'\n'.join(label+': '+str(profile[key]) for key,label in
            [('rfc','RFC'),('name','Nombre'),('postal_code','CP fiscal'),('fiscal_regime','Régimen'),('cfdi_use','Uso CFDI'),('email','Correo')])
        text+='\nLa captura en este portal sigue siendo manual. No se ha solicitado la factura.'
        await self.client.send(chat_id,text)

    def _permission_buttons(self,permission):
        return [('Permitir','wa:'+permission['id']),('Cancelar','wx:'+permission['id'])]

    async def _portal_permission(self,owner,chat_id,identifier):
        permission=self.billing.store.get('portal_permission',owner,identifier)
        ticket=self.billing.resolve_ticket(owner,permission['ticket_id'])
        chain=list(ticket.get('portal_domain_chain') or [portal_origin(self.billing.portal_memory.entry_url(owner,ticket))])
        targets=[value for value in permission['origins'] if value!=chain[-1]]
        text='¿Permites revisar este portal?\n'+'\n'.join(permission['origins'])
        for target in targets:
            text+='\nRuta: '+' → '.join(urlsplit(value).hostname for value in [*chain,target])
        if permission.get('route_version')==1:
            text+='\nIncluye recorrer este dominio. Cada nuevo dominio, también para recursos, pedirá confirmación.'
        text+='\nSolo navegación pública; sin enviar datos fiscales ni facturar.'
        if permission['intent']=='browse':
            text+=' La revisión con IA consume tokens.'
        await self.client.send(chat_id,text,self._permission_buttons(permission))

    async def _run_browser(self,owner,chat_id,identifier):
        ticket=self.billing.resolve_ticket(owner,identifier)
        if not self.billing.browser.enabled:
            raise AppError(503,'browser_disabled','La navegación aún no está activada por el operador.')
        if not self.billing.portal_permissions.route_allowed(owner,ticket):
            permission=self.billing.portal_permissions.request(owner,ticket['id'],route=True)
            await self._portal_permission(owner,chat_id,permission['id'])
            return
        state=self._state(owner)
        state.update(last_ticket_id=ticket['id'],browser_ticket_id=ticket['id'])
        self._save_state(owner,state)
        try:
            record=self.billing.browser_review(owner,ticket['id'])
        except AppError as error:
            if error.status!=404:
                raise
            record=None
        if record and public_form_ready(record['snapshot']):
            await self._browser(owner,chat_id,ticket['id'])
            return
        choice=state.get('browser_choice') or {}
        selected=choice.get('label') if choice.get('ticket_id')==ticket['id'] and choice.get('scope')==self.billing.portal_permissions.scope(ticket) and choice.get('expires',0)>time.time() else None
        # Solo retoma una elección explícita del usuario tras autorizar su destino.
        # Continuar con IA debe dejar investigar al agente, no imponer un logo.
        action=matching_brand(ticket,record['snapshot'],label=selected) if record and selected else None
        if action:
            await self._brand(owner,chat_id,ticket['id'],record['snapshot']['observation_id'],action['reference'])
            return
        if not self.agent:
            raise AppError(503,'telegram_agent','El agente de Telegram no está configurado.')
        try:
            await self.agent.reply(owner,chat_id,portal_review=True,text='Investiga el acceso de facturación del ticket seleccionado. Observa y sigue los controles útiles hasta el formulario o una ambigüedad real, dentro del presupuesto disponible. Usa los permisos vigentes; cada nuevo dominio documental requiere el botón de permiso humano. Elige la marca coincidente. No introduzcas datos ni solicites facturas.')
        except AppError as error:
            if error.code not in {'agent_turn_limit','tool_limit','run_timeout','context_limit'}:
                raise
            try:
                snapshot=self.billing.browser_review(owner,ticket['id'])['snapshot']
                ready=public_form_ready(snapshot) or unresolved_brand_selector(ticket,snapshot)
            except AppError as saved_error:
                if saved_error.status!=404:
                    raise
                ready=False
            if not ready:
                await self.client.send(chat_id,'Revisión pausada por el límite de ejecución.')
            await self._browser(owner,chat_id,ticket['id'])

    async def _refresh_browser(self,owner,chat_id,identifier):
        try:
            await self.billing.browse_portal(owner,identifier)
        except PortalPermissionRequired as error:
            permission=self.billing.portal_permissions.request(owner,identifier,origins=error.origins)
            await self._portal_permission(owner,chat_id,permission['id'])
            return
        await self._browser(owner,chat_id,identifier)

    async def _brand(self,owner,chat_id,identifier,observed,reference):
        ticket=self.billing.resolve_ticket(owner,identifier)
        try:
            record=self.billing.browser_review(owner,ticket['id'])
        except AppError as error:
            if error.status!=404:
                raise
            await self._refresh_browser(owner,chat_id,ticket['id'])
            return
        action=next((item for item in record['snapshot']['actions'] if item['reference']==reference),None)
        if observed!=record['snapshot']['observation_id'] or not action:
            await self._refresh_browser(owner,chat_id,ticket['id'])
            return
        try:
            await self.billing.browse_portal(owner,ticket['id'],observation_id=observed,reference=reference)
        except PortalPermissionRequired as error:
            state=self._state(owner)
            state['browser_choice']={'ticket_id':ticket['id'],'scope':self.billing.portal_permissions.scope(ticket),
                'label':action['label'],'expires':record['_expires']}
            self._save_state(owner,state)
            permission=self.billing.portal_permissions.request(owner,ticket['id'],origins=error.origins)
            await self._portal_permission(owner,chat_id,permission['id'])
            return
        except AppError as error:
            if error.code not in {'browser_stale','browser_session'}:
                raise
            await self._refresh_browser(owner,chat_id,ticket['id'])
            return
        state=self._state(owner)
        state.pop('browser_choice',None)
        self._save_state(owner,state)
        await self._browser(owner,chat_id,ticket['id'])

    async def _browser(self,owner,chat_id,identifier,_alternative_depth=0):
        ticket=self.billing.resolve_ticket(owner,identifier)
        try:
            record=self.billing.browser_review(owner,ticket['id'])
        except AppError as error:
            if error.status!=404:
                raise
            failure=ticket.get('portal_browser_error') or {}
            buttons=[('Ver ticket','ts:'+ticket['id'])]
            if failure.get('code') not in {'browser_origin','browser_configuration','browser_disabled','browser_protocol','browser_boundary'}:
                buttons.insert(0,('Revisar portal con IA','bn:'+ticket['id']))
            await self.client.send(chat_id,failure.get('message') or 'La revisión venció o no pudo abrirse.',buttons)
            return
        snapshot=record['snapshot']
        ready=public_form_ready(snapshot)
        preparation=self.billing.portal_preparation
        if ready and hasattr(self.billing.extractor,'extract_fields'):
            try:
                changed=await preparation.read_fields(owner,ticket['id'],snapshot)
                if changed:
                    updated=self.billing.resolve_ticket(owner,ticket['id'])
                    if updated['id']!=ticket['id']:
                        self.workflow.activate(owner,updated)
                        await self.client.send(chat_id,'Este ticket ya existe. Retomo su solicitud guardada.')
                        await self._ticket(owner,chat_id,updated['id'])
                        return
                    ticket=updated
                if _alternative_depth<2:
                    alternate=await preparation.advance_alternative(owner,ticket['id'],snapshot)
                    if alternate:
                        await self._browser(owner,chat_id,ticket['id'],_alternative_depth+1)
                        return
            except PortalPermissionRequired as error:
                permission=self.billing.portal_permissions.request(owner,ticket['id'],origins=error.origins)
                await self._portal_permission(owner,chat_id,permission['id'])
                return
            except AppError as error:
                if error.code not in {'portal_source_expired','billing_daily_limit','billing_busy','extraction_attempted',
                        'portal_not_ticket','portal_source_mismatch','portal_changed','extraction_auth','extraction_quota','extraction_provider','extraction_timeout','extraction_format','extraction_failure'}:
                    raise
                ticket['portal_data_error']={'code':error.code,'message':error.message}
        profile=self.billing.store.get('profile',owner,ticket['profile_id'])
        form_values,form_missing=preparation.plan(owner,ticket,snapshot) if ready else ([],[])
        active_fields=preparation.active_fields(ticket,snapshot) if ready else snapshot['fields']
        prepared_values={item['reference']:item['value'] for item in form_values}
        try:
            proposal=self.billing.portal_memory.propose(owner,ticket['id'])
        except AppError:
            proposal=None
        text=('Formulario encontrado\n' if ready else 'Revisión pública\n')+(proposal['url'] if proposal else snapshot['page'])
        if ticket.get('total'):text+='\nImporte del ticket: '+str(ticket['total'])
        elif ready:text+='\nNo leí el importe. Escríbelo o envía foto de ese campo.'
        if ticket.get('portal_browser_error'):
            text+='\n'+ticket['portal_browser_error']['message']
        if ticket.get('portal_data_error'):
            text+='\n'+ticket['portal_data_error']['message']
        if snapshot.get('login_required'):
            text+='\nEl portal requiere iniciar sesión. Hace falta intervención humana.'
        elif snapshot['fields']:
            text+='\nDatos para el portal:' if ready else '\nCampos visibles; puede haber alternativas:'
            for item in active_fields:
                key=item.get('key')
                value=prepared_values.get(item.get('reference')) or ticket.get(key) or profile.get(key) or 'No leído. Complétalo abajo'
                text+='\n'+item['label']+': '+str(value)
        else:
            text+='\nAún no se encontró el formulario.'
        if snapshot.get('partial'):
            text+='\nLa vista es parcial; algunos controles u opciones no se incluyeron.'
        pending=[target for target in snapshot['blocked_origins'] if target not in self.billing.portal_permissions.allowed(owner,ticket)]
        awaiting_form=bool(pending and record.get('brand_label') and not has_billing_form(snapshot))
        brands=brand_options(snapshot) if not has_billing_form(snapshot) and not snapshot['captcha'] and not snapshot.get('login_required') and not snapshot['network_blocked'] and not awaiting_form else []
        preferred=matching_brand(ticket,snapshot) if not snapshot.get('login_required') else None
        if snapshot.get('brand_selection') and not brands and not preferred and not snapshot['fields']:
            text+='\nEl selector de marcas está visible, pero no se detectaron sus opciones.'
        if brands:
            text+='\nPuedes elegir la marca o continuar la revisión con IA.'
        if snapshot['captcha']:
            text+='\nEl sitio requiere CAPTCHA; intervención humana.'
        needs_resources=bool(pending) and (awaiting_form or snapshot['network_blocked'] or not (snapshot['fields'] or snapshot['actions']))
        if snapshot['network_blocked']:
            reasons={'unsupported_request':'El sitio intentó enviar una solicitud no habilitada.',
                'unsupported_document':'El destino es un archivo o respuesta sin formulario HTML.',
                'request_limit':'Se alcanzó el límite de peticiones del recorrido.',
                'page_request_limit':'Se alcanzó el límite de peticiones de esta página.',
                'size_limit':'Se alcanzó el límite de tamaño del recorrido.',
                'page_size_limit':'Esta página supera el presupuesto de tamaño.',
                'response_size_limit':'Un archivo del portal supera el tamaño admitido.',
                'origin_limit':'Se alcanzó el límite de dominios del recorrido.',
                'navigation_failed':'Falló la carga del destino.',
                'timeout':'El portal tardó demasiado en responder.',
                'network_error':'Falló una conexión del portal.',
                'dns_error':'No se pudo resolver la dirección del portal.',
                'tls_error':'El certificado HTTPS del portal no se pudo validar.',
                'private_address':'El destino no es una dirección pública admitida.',
                'http_error':'El portal devolvió un error HTTP.',
                'permission':'Falta autorizar el destino de navegación.'}
            text+='\n'+reasons.get(snapshot.get('block_reason'),'Se bloqueó la carga del destino; revisa el diagnóstico.')
        if needs_resources:
            text+='\n¿Permites cargar estos recursos del sitio?\n'+'\n'.join(pending)
        if ready:
            missing_labels=list(dict.fromkeys(label.rstrip('* ').strip() for label in form_missing))
            if missing_labels:
                text+='\nFalta: '+', '.join(missing_labels)+'.'
            text+='\nNo se solicitó la factura.'
        else:
            text+='\nRevisar con IA consume tokens. No se enviaron datos fiscales ni se solicitó la factura.'
        buttons=[]
        if ready:
            if not ticket.get('total'):buttons.append(('Completar importe','td:'+ticket['id']+':total'))
            if ticket.get('portal_fill'):
                buttons.append(('Ver estado del llenado','xs:'+ticket['portal_fill']['review_id']))
            if hasattr(self.billing.browser,'transaction_view'):
                buttons.append(('Continuar facturación','lv:'+ticket['id']))
            if not form_missing and form_values and hasattr(self.billing.browser,'fill'):
                fill_review=preparation.fill_request(owner,ticket['id'],snapshot)
                if fill_review:
                    text+='\nLlenar copia estos datos al navegador, con la red cerrada y sin enviar.'
                    buttons.append(('Llenar formulario','xg:'+fill_review['id']))
            if hasattr(self.billing.extractor,'extract_fields') and (preparation.replacement_pending(owner,ticket)
                    or any(not ticket.get(item['key']) for item in preparation.receipt_fields(snapshot,ticket))):
                try:
                    analysis=preparation.analysis_request(owner,ticket['id'],snapshot)
                except AppError as error:
                    if error.code!='portal_source_expired':raise
                    analysis=None
                    buttons.append(('Reenviar foto de este ticket','xu:'+ticket['id']))
                if analysis:
                    text+='\nLeer los campos del portal comparte este ticket con OpenAI y usa tokens.'
                    buttons.append(('Leer datos que pide el portal','xa:'+analysis['id']))
                elif not any(action.startswith('xu:') for _,action in buttons):
                    buttons.append(('Reenviar foto de este ticket','xu:'+ticket['id']))
            correctable={'web_id':'Web ID','store_number':'Tienda','register_number':'Caja','toll_station':'caseta','lane':'carril','purchase_time':'hora',
                'account_number':'Cuenta','folio':'ticket/folio','reference_number':'referencia','billing_code':'código',
                'purchase_date':'fecha','total':'importe'}
            keys=list(dict.fromkeys(item.get('key') for item in active_fields))
            buttons.extend(('Corregir '+correctable[key],'xe:'+ticket['id']+':'+key)
                for key in keys if key in correctable and ticket.get(key))
            buttons.extend(('Usar alternativa: '+action['label'][:40],'ba:'+ticket['id']+':'+snapshot['observation_id']+':'+action['reference'])
                for action in preparation.alternatives(ticket,snapshot)[:4])
        if snapshot['network_blocked'] and not snapshot['captcha'] and not snapshot.get('login_required') and snapshot.get('block_reason') in RESUMABLE_LIMITS and self.billing.portal_resume_url(owner,ticket):
            text+='\nPuedes retomar desde esta página.'
            buttons.append(('Retomar desde esta página','bn:'+ticket['id']))
        choices=[preferred] if preferred else brands[:8]
        for item in choices:
            buttons.append(('Abrir '+item['label'][:45],'ba:'+ticket['id']+':'+snapshot['observation_id']+':'+item['reference']))
        if proposal:
            buttons.append(('Guardar portal de '+proposal['merchant'][:35],'bs:'+ticket['id']+':'+snapshot['observation_id']))
        if needs_resources:
            permission=self.billing.portal_permissions.request(owner,ticket['id'],origins=pending)
            buttons.extend(self._permission_buttons(permission))
        missing=browser_missing_fields(ticket)
        if missing and not ticket.get('delivery'):
            labels={'web_id':'Web ID','store_number':'Tienda','register_number':'Caja','toll_station':'caseta','lane':'carril','purchase_time':'hora',
                'account_number':'Cuenta','folio':'folio','reference_number':'referencia','billing_code':'código',
                'purchase_date':'fecha','total':'importe'}
            buttons.extend(('Completar '+labels[key],'td:'+ticket['id']+':'+key) for key in missing[:6])
            try:
                source=self.billing.document(owner,ticket['document_id'])
            except AppError:
                source=None
            if source and source.get('extraction_version')!=EXTRACTION_VERSION:
                buttons.append(('Autorizar lectura de datos faltantes','ra:'+source['id']))
        if not ready and not unresolved_brand_selector(ticket,snapshot) and snapshot['steps_remaining'] and snapshot['actions'] and not snapshot['captcha'] and not snapshot.get('login_required') and not snapshot['network_blocked']:
            buttons.append(('Continuar revisión con IA','bn:'+ticket['id']))
        if not ready:
            text+='\nMe quedé en esta página. Puedes escribir la dirección de facturación para continuar.'
            buttons.append(('Escribir dirección de facturación','dw:'+ticket['id']))
        if (snapshot.get('partial') or snapshot.get('block_reason') or (snapshot.get('diagnostics') or {}).get('issues')
                or snapshot.get('brand_selection') and not brands and not preferred):
            buttons.append(('Ver diagnóstico (sin IA)','bd:'+ticket['id']))
        buttons.append(('Ver estado del ticket','ts:'+ticket['id']))
        await self.client.send(chat_id,text,buttons+self.workflow.navigation(owner))

    async def _browser_diagnostic(self,owner,chat_id,identifier):
        ticket=self.billing.resolve_ticket(owner,identifier)
        try:
            saved=self.billing.browser_review(owner,ticket['id'])['snapshot']
        except AppError as error:
            if error.status!=404:
                raise
            saved=ticket.get('portal_browser') or {}
        report=saved.get('diagnostics') or {}
        if not report:
            await self.client.send(chat_id,'Sin diagnóstico guardado. Abre otra revisión del portal.')
            return
        text='Diagnóstico guardado · sin IA\n'
        text+=f"Descargado: {report['transferred_bytes']/1048576:.2f} MiB. Página: {report['page_bytes']/1048576:.2f} MiB.\n"
        text+=f"Peticiones: {report['requests']}. Recursos reutilizados: {report['cache_hits']}."
        if saved.get('brand_selection') and 'actions' in saved:
            text+='\nSelector: '+str(len(brand_options(saved)))+' opciones de marca; '+str(len(saved['actions']))+' controles públicos.'
        if saved.get('omitted_writes'):
            text+='\nSolicitudes de envío bloqueadas: '+str(saved['omitted_writes'])+'.'
        if saved.get('partial'):
            text+='\nVista parcial de controles y opciones.'
        labels={'response_size_limit':'archivo demasiado grande','size_limit':'presupuesto total agotado',
            'page_size_limit':'presupuesto de página agotado','http_error':'error HTTP','timeout':'tiempo de espera agotado',
            'network_error':'fallo de conexión','private_address':'dirección privada denegada','dns_error':'fallo DNS',
            'tls_error':'fallo de certificado HTTPS','redirect_cookie':'redirección de recurso con estado no compatible',
            'unexpected_content':'el recurso no devolvió el formato esperado','load_failed':'falló la carga',
            'csp':'bloqueo de la política de seguridad del sitio','javascript_error':'falló un script de la página',
            'cors':'el sitio bloqueó el recurso por CORS','unsupported_document':'destino sin formulario HTML'}
        for issue in report.get('issues',[])[:4]:
            text+='\n'+issue['kind']+': '+labels.get(issue['code'],'recurso no disponible')
            if issue.get('status'):
                text+=' '+str(issue['status'])
            if issue.get('site'):
                text+=' · '+issue['site']
        await self.client.send(chat_id,text,[('Ver ticket','ts:'+ticket['id'])])

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
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        ticket_id=ticket['id']
        state=self._state(owner)
        state['last_ticket_id']=ticket_id
        self._save_state(owner,state)
        approval=self.billing.request_email_send_approval(owner,ticket_id,include_csf=csf,authorize_csf=csf,user_requested_csf=user_requested)
        await self._email_review(owner,chat_id,approval['id'])

    async def _email_review(self,owner,chat_id,identifier):
        approval=self.billing.store.get('email_approval',owner,identifier)
        state=self._state(owner)
        state['last_ticket_id']=approval['ticket_id']
        self._save_state(owner,state)
        if approval['_state']!='pending':
            raise AppError(409,'billing_approval','La autorización caducó o ya fue utilizada.')
        text=f"Revisa antes de enviar\nPara: {approval['to']}\nAsunto: {approval['subject']}\nAdjuntos: "+', '.join(item['label'] for item in approval['attachments'])+'\n\n'+approval['body']
        if approval.get('csf_share_reason')=='user_requested':
            text+='\nSe compartirá tu CSF completa por tu elección.'
        # Nunca ofrecer aprobar una revisión recortada por el límite de Telegram.
        if len(text)>3900:
            await self.client.send(chat_id,'El borrador es demasiado largo para revisarlo completo en Telegram. Recházalo y solicita una revisión manual.',
                [('Rechazar envío','no:'+approval['id'])]+self.workflow.navigation(owner))
        else:
            if not self.settings.billing_live_email_enabled or not self.billing.mail.ready(owner):
                text+='\nVista previa: falta activar el envío o conectar Gmail.'
                await self.client.send(chat_id,text,[('Rechazar borrador','no:'+approval['id'])]+self.workflow.navigation(owner))
            else:
                await self.client.send(chat_id,text,[('Confirmar y enviar este correo','se:'+approval['id']),('Rechazar envío','no:'+approval['id'])]+self.workflow.navigation(owner))

    async def show_review(self,owner,chat_id,review):
        identifier=review.get('id','')
        kind=review.get('kind')
        if kind=='profile_setup':
            await self.profiles.resume(owner,chat_id)
        elif kind=='email':
            await self._email_review(owner,chat_id,identifier)
        elif kind=='ticket':
            await self._ticket(owner,chat_id,identifier)
        elif kind=='ticket_list':
            await self.requests.show(owner,chat_id)
        elif kind=='browser':
            await self._browser(owner,chat_id,identifier)
        elif kind=='portal_permission':
            await self._portal_permission(owner,chat_id,identifier)
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
        if any(self.billing.store.find(kind,owner,lookup) for kind in ('channel_update','channel_control')):
            return {'ok':True,'duplicate':True}
        lock=self._locks.setdefault(user_id,asyncio.Lock())
        if lock.locked():
            await self.client.send(user_id,'Estoy procesando tu solicitud anterior. Espera la respuesta y vuelve a enviar este mensaje.')
            return {'ok':True,'busy':True}
        async with lock:
            return await self._handle_authorized(owner,user_id,update_id,lookup,message,callback)

    def _pending_review_control(self,owner,data):
        """Solo resolver una revisión propia ya guardada usa la cuota de controles."""
        if not isinstance(data,str):return False
        prefix,separator,tail=data.partition(':')
        identifier,_,action=tail.partition(':')
        if not separator or (action and prefix!='fw'):return False
        try:
            uuid.UUID(identifier)
            if prefix in {'ga','gr'}:
                state=self.billing.store.find('channel_state',owner,'telegram') or {}
                if not state.get('conversation_id'):return False
                row=self.billing.database.get_conversation(state['conversation_id'],owner)
                pending=json.loads(row['pending'] or 'null')
                return bool(pending and pending['id']==identifier and pending['expires']>time.time())
            if prefix=='pd':
                self.billing.store.get('profile_draft',owner,identifier)
                return any(review['_state']=='pending' and review.get('draft_id')==identifier
                    for review in self.billing.store.list('profile_review',owner))
            if prefix=='sv':
                # SAT no tiene un registro de aprobación separado: la revisión está en el ticket.
                ticket=self.billing.store.get('ticket',owner,identifier)
                validation=ticket.get('validation')
                return bool(validation and not validation.get('sat_verified'))
            kinds={'ls':'portal_send_review','se':'email_approval','no':'email_approval',
                'pf':'profile_review','xa':'portal_data_review','xg':'portal_fill_review',
                'la':'portal_field_photo','lu':'portal_field_photo','wa':'portal_permission','fw':'workflow_choice'}
            if prefix not in kinds:return False
            review=self.billing.store.get(kinds[prefix],owner,identifier)
            if prefix=='lu':return review['_state']=='read' and bool(review.get('value'))
            if prefix=='fw' and action not in {'same','other','keep','pause','cancel','resume'}:return False
            return review['_state']=='pending'
        except (AppError,ValueError,TypeError,KeyError):
            return False

    async def _handle_authorized(self,owner,user_id,update_id,lookup,message,callback):
        plain=normalized(message.get('text',''))
        callback_data=callback.get('data','')
        menu_control=isinstance(callback_data,str) and callback_data.startswith('mn:') and callback_data.rsplit(':',1)[-1] in {'home','list','profile','continue'}
        control=(not callback and not (message.get('document') or message.get('photo'))
            and plain in REQUEST_TEXTS|{'/tickets','/limites','/portales','/menu','/start','/ayuda','/inicio'}) or menu_control or (isinstance(callback_data,str) and callback_data.startswith(('ts:','wx:','bf:','bd:')))
        if callback and not control:
            control=self._pending_review_control(owner,callback_data)
        kind='channel_control' if control else 'channel_update'
        today=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        with self.billing.database.connection() as conn:
            used=conn.execute('SELECT COUNT(*) FROM billing_records WHERE kind=? AND owner=? AND created>=?',(kind,owner,today)).fetchone()[0]
        maximum=500 if control else self.settings.billing_max_daily_telegram_updates
        if used>=maximum:
            notice=self.billing.store.digest('telegram-limit:'+kind+':'+str(today))
            if not self.billing.store.find('channel_notice',owner,notice):
                self.billing.store.add('channel_notice',owner,{},lookup=notice,expires=today+86400)
                if control:
                    await self.client.send(user_id,'Límite de consultas de estado alcanzado por hoy. Tus datos se conservan.')
                else:
                    await self.requests.limits(owner,user_id,code='telegram_daily_limit')
            return {'ok':True,'rate_limited':True}
        # Persistir antes de cualquier efecto evita duplicados en reenvíos del webhook.
        self.billing.store.add(kind,owner,{},lookup=lookup,expires=time.time()+30*86400)
        state=self._state(owner)
        state['chat_id']=user_id
        self._save_state(owner,state)
        try:
            if callback:
                await self._callback(owner,user_id,callback.get('data',''))
                return {'ok':True}
            await self._message(owner,user_id,message)
        except AppError as error:
            await self.requests.error(owner,user_id,error)
        except Exception:
            await self.client.send(user_id,'No se completó la operación. Revisa el estado antes de repetir; no se reintentará automáticamente.')
        return {'ok':True}

    async def _callback(self,owner,chat_id,data):
        if not isinstance(data,str):
            raise AppError(422,'telegram_callback','Acción no válida.')
        if await self.workflow.callback(owner,chat_id,data):
            return
        prefix,separator,identifier=data.partition(':')
        if not separator:
            raise AppError(422,'telegram_callback','Acción no válida.')
        code=None
        if prefix in {'cu','rf','td','xe','ba','bs','lm'}:
            identifier,_,code=identifier.partition(':')
        try:
            uuid.UUID(identifier)
        except ValueError:
            raise AppError(422,'telegram_callback','Acción no válida.') from None
        if await self.workflow.guard(owner,chat_id,prefix,identifier):
            return
        if prefix in {'lv','lr','ls','lq','lm','la','lu'}:
            await self.submission.callback(owner,chat_id,prefix,identifier,code)
        elif prefix in {'td','xe'}:
            ticket=self.billing.store.get('ticket',owner,identifier)
            allowed=set(pending_receipt_fields(ticket))|set(browser_missing_fields(ticket))
            if prefix=='xe':
                allowed={item['key'] for item in (ticket.get('portal_browser') or {}).get('fields',[])} & {
                    'account_number','folio','reference_number','billing_code','web_id','store_number','register_number','toll_station','lane','purchase_time','purchase_date','total'}
            if code not in allowed or ticket.get('delivery'):
                raise AppError(409,'ticket_field','Ese dato no está pendiente.')
            state=self._state(owner)
            state['ticket_input']={'id':identifier,'field':code,'correction':prefix=='xe'}
            self._save_state(owner,state)
            examples={'merchant':'COMERCIO','purchase_date':'AAAA-MM-DD','folio':'FOLIO','total':'IMPORTE',
                'account_number':'CUENTA','reference_number':'REFERENCIA','billing_code':'CODIGO',
                'web_id':'WEB_ID','store_number':'TIENDA','register_number':'CAJA','toll_station':'CASETA','lane':'CARRIL','purchase_time':'HORA'}
            await self.client.send(chat_id,'No leí '+examples[code]+'. Escríbelo o envía una foto de ese campo.')
        elif prefix=='xa':
            await self.billing.portal_preparation.read_approved(owner,identifier)
            review=self.billing.store.get('portal_data_review',owner,identifier)
            if review.get('receipt_update'):
                await self.receipts.after_read(owner,chat_id,review)
                return
            ticket=self.billing.resolve_ticket(owner,review['ticket_id'])
            try:
                await self.billing.portal_preparation.current(owner,ticket['id'])
            except AppError:
                await self._ticket(owner,chat_id,ticket['id'])
                return
            await self._browser(owner,chat_id,ticket['id'])
        elif prefix=='xu':
            ticket=self.billing.resolve_ticket(owner,identifier)
            state=self._state(owner)
            state['portal_upload_ticket']=ticket['id']
            self._save_state(owner,state)
            await self.client.send(chat_id,'Envía otra foto o PDF de este mismo ticket. Tu perfil se conserva.')
        elif prefix=='xr':
            await self.billing.portal_preparation.current(owner,identifier,refresh=True)
            await self._browser(owner,chat_id,identifier)
        elif prefix=='xg':
            try:
                review=await self.billing.portal_preparation.fill_approved(owner,identifier)
            except AppError as error:
                try:
                    saved=self.billing.store.get('portal_fill_review',owner,identifier)
                except AppError:
                    raise error
                if saved['_state']=='rejected':
                    await self.client.send(chat_id,error.message,[('Actualizar formulario','xr:'+saved['ticket_id']),('Ver ticket','ts:'+saved['ticket_id'])])
                    return
                if saved['_state']!='unknown':raise
                await self.client.send(chat_id,'No se confirmó el llenado. Consulta su estado antes de repetir.',
                    [('Ver estado del llenado','xs:'+identifier)])
                return
            await self.client.send(chat_id,'Formulario llenado en el navegador. No se envió ni se solicitó la factura.',
                [('Ver estado del llenado','xs:'+review['id'])]+
                ([('Solicitar factura en el portal','lv:'+review['ticket_id'])] if hasattr(self.billing.browser,'transaction_view') else [])+self.workflow.navigation(owner))
        elif prefix=='xs':
            try:
                result=await self.billing.portal_preparation.fill_status(owner,identifier)
            except AppError as error:
                if error.code!='browser_session':raise
                review=self.billing.store.get('portal_fill_review',owner,identifier)
                await self.client.send(chat_id,'La sesión de llenado venció. Los datos se conservan; no se solicitó la factura.',
                    [('Revisar formulario','bn:'+review['ticket_id'])])
                return
            if result.get('expired'):
                await self.client.send(chat_id,'La revisión del llenado venció. Tus datos se conservan; no se solicitó la factura.',
                    [('Revisar formulario','xr:'+result['ticket_id'])])
                return
            await self.client.send(chat_id,'Formulario preparado; sesión activa '+str(result['expires_in'])+' s. Sin envío al comercio.',self.workflow.navigation(owner))
        elif prefix=='ts':
            await self._ticket(owner,chat_id,identifier)
        elif prefix=='df':
            await self.discovery.search(owner,chat_id,identifier)
        elif prefix=='dw':
            await self.discovery.ask_site(owner,chat_id,identifier)
        elif prefix=='bd':
            await self._browser_diagnostic(owner,chat_id,identifier)
        elif prefix=='er':
            await self._email_review(owner,chat_id,identifier)
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
            if document.get('duplicate_ticket_id') or (document['purpose']=='ticket' and not document.get('superseded_by')
                    and self.billing.resolve_ticket(owner,document['ticket_id'])['status'] not in {'TICKET_RECIBIDO','ERROR'}):
                await self._ticket(owner,chat_id,document['ticket_id'])
                return
            if document['purpose']=='unclassified' or document.get('intended_purpose')=='ticket' or document.get('superseded_by'):
                await self._document_choice(owner,chat_id,identifier,reason='Primero confirma el tipo y el perfil del archivo; el botón anterior ya no corresponde a su estado actual.')
                return
            if document['purpose']=='csf':
                if any(item.get('document_id')==identifier for item in self.billing.store.list('profile',owner)) and not self._state(owner).get('replace_profile_id'):
                    await self.profiles.resume(owner,chat_id)
                    return
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
                if document.get('analysis_scope_offered') in {'portal-fields-v1','portal-fields-v2','portal-fields-v3'}:
                    document['analysis_scope']=document['analysis_scope_offered']
                    self.billing.store.update('document',owner,identifier,document)
                ticket=await self.billing.analyze_ticket(owner,identifier,consent=True)
                self.workflow.activate(owner,self.billing.resolve_ticket(owner,ticket['id']))
                if ticket.get('duplicate'):
                    await self.client.send(chat_id,'Este ticket ya existe. Retomo su solicitud guardada.')
                state=self._state(owner)
                state['last_ticket_id']=ticket['id']
                self._save_state(owner,state)
                await self.discovery.after_analysis(owner,chat_id,ticket,self.billing.document(owner,identifier))
        elif prefix=='ra':
            document=self.billing.retry_ticket_after_update(owner,identifier)
            state=self._state(owner)
            state.update(last_document_id=document['id'],last_ticket_id=self.billing.document(owner,document['id'])['ticket_id'])
            self._save_state(owner,state)
            ticket=await self.billing.analyze_ticket(owner,document['id'],consent=True)
            self.workflow.activate(owner,self.billing.resolve_ticket(owner,ticket['id']))
            await self._ticket(owner,chat_id,ticket['id'])
        elif prefix=='bp':
            await self._portal(owner,chat_id,identifier)
        elif prefix=='bn':
            await self._run_browser(owner,chat_id,identifier)
        elif prefix=='ba':
            observed,_,reference=(code or '').partition(':')
            if len(observed)!=8 or len(reference)!=8:
                raise AppError(422,'telegram_callback','Acción no válida.')
            await self._brand(owner,chat_id,identifier,observed,reference)
        elif prefix=='bs':
            if not code or len(code)!=8:
                raise AppError(422,'telegram_callback','Acción no válida.')
            saved=self.billing.portal_memory.save(owner,identifier,code)
            await self.client.send(chat_id,'Portal de '+saved['merchant']+' guardado para próximos tickets.')
        elif prefix in {'bm','bo'}:
            ticket=self.billing.portal_memory.select(owner,identifier,remembered=prefix=='bm')
            await self._ticket(owner,chat_id,ticket['id'])
        elif prefix=='bf':
            self.billing.portal_memory.forget(owner,identifier)
            await self.client.send(chat_id,'Enlace olvidado. Se usará el portal del ticket.')
        elif prefix in {'wa','wx'}:
            permission=self.billing.portal_permissions.decide(owner,identifier,approve=prefix=='wa')
            await self.billing.sync_portal_permissions(owner,permission['ticket_id'],cancel=prefix=='wx')
            if prefix=='wx':
                state=self._state(owner)
                if (state.get('browser_choice') or {}).get('ticket_id')==permission['ticket_id']:
                    state.pop('browser_choice',None)
                    self._save_state(owner,state)
                await self.client.send(chat_id,'Revisión cancelada. Tu perfil y ticket se conservan.')
            elif permission['intent']=='details':
                await self._portal(owner,chat_id,permission['ticket_id'])
            else:
                await self._run_browser(owner,chat_id,permission['ticket_id'])
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
            approval=self.billing.store.get('email_approval',owner,identifier)
            state=self._state(owner)
            state['last_ticket_id']=approval['ticket_id']
            self._save_state(owner,state)
            ticket=await self.billing.send_invoice_request_email(owner,identifier,confirmed=True)
            await self.client.send(chat_id,'Solicitud '+ticket['id'][:8]+' enviada. Esperando la factura.',
                [('Ver solicitud','ts:'+ticket['id'])]+self.workflow.navigation(owner))
        elif prefix=='no':
            self.billing.reject_email(owner,identifier)
            await self.client.send(chat_id,'Envío rechazado; no se envió el correo.',self.workflow.navigation(owner))
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
        if await self.receipts.before(owner,chat_id,message):return
        if await self.workflow.message(owner,chat_id,message):
            return
        if await self.discovery.message(owner,chat_id,message):return
        if await self.submission.message(owner,chat_id,message):
            return
        text=message.get('text','').strip()
        declaration=text.casefold().strip(' .!¡?¿')
        state=self._state(owner)
        profiles=self.billing.store.list('profile',owner)
        if text in {'/start','/ayuda'}:
            draft,csf=self._profile_setup(owner,state) if not profiles else (None,None)
            status='Perfil listo. Envía tu ticket.' if profiles else ('Completa tu perfil aquí: /perfil.' if draft or csf else 'Envía tu CSF una vez. Después, tus tickets.')
            await self.client.send(chat_id,status)
            if text=='/ayuda':
                await self.client.send(chat_id,'/perfil · /estado · /tickets · /limites\n/portales · /documentos · /nuevo · /historial\nLa IA consume tokens.')
        elif normalized(text) in REQUEST_TEXTS:
            await self.requests.show(owner,chat_id)
        elif normalized(text)=='/limites':
            await self.requests.limits(owner,chat_id)
        elif normalized(text)=='/portales':
            records=self.billing.store.list('portal_memory',owner)
            if not records:
                await self.client.send(chat_id,'No hay portales guardados todavía.')
            for record in records[:50]:
                await self.client.send(chat_id,record['merchant']+'\n'+record['url'],[('Olvidar enlace','bf:'+record['id'])])
        elif text=='/nuevo_chat' and self.agent:
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
            await self.requests.list(owner,chat_id)
        elif text=='/conectar_gmail':
            url=self.billing.mail.connect(owner)
            await self.client.send(chat_id,'Autoriza en Google el envío y la lectura de este buzón. No compartas contraseñas ni tokens aquí. Después vuelve a Telegram.\n'+url)
        elif text.startswith('/requiere_csf '):
            parts=text.split(maxsplit=2)
            if len(parts)!=3:
                raise AppError(422,'csf_request','Usa /requiere_csf ID_TICKET descripción de la petición expresa del comercio.')
            if await self.workflow.guard(owner,chat_id,'pr',parts[1]):
                return
            self.billing.mark_certificate_required(owner,parts[1],confirmed=True,evidence=parts[2])
            await self.client.send(chat_id,'Petición de constancia registrada. Solo se adjuntará si autorizas /preparar_con_csf ID_TICKET y luego confirmas el correo completo.')
        elif text.startswith('/preparar_con_csf '):
            identifier=text.split(maxsplit=1)[1]
            if await self.workflow.guard(owner,chat_id,'pr',identifier):
                return
            await self._preview(owner,chat_id,identifier,csf=True)
        elif message.get('document') or message.get('photo'):
            caption=message.get('caption','').strip()
            attachment=message.get('document') or message['photo'][-1]
            if attachment.get('file_size',0)>self.settings.billing_max_upload_bytes:
                raise AppError(413,'document_size','El archivo supera el límite permitido.')
            data=await self.client.download(attachment['file_id'])
            active=self.workflow.active(owner)
            state=self._state(owner)
            explicit_csf=caption.casefold().startswith('/csf') or state.get('replace_profile_id') or state.get('expected_upload')=='csf'
            if await self.receipts.receive(owner,chat_id,message,data,explicit_csf=explicit_csf):return
            if state.get('portal_upload_ticket') and (explicit_csf or not active or active['id']!=state['portal_upload_ticket']):
                state.pop('portal_upload_ticket',None)
                self._save_state(owner,state)
            if state.get('portal_upload_ticket'):
                ticket=self.billing.resolve_ticket(owner,state['portal_upload_ticket'])
                document=self.billing.receive_document(owner,'unclassified',data,profile_id=ticket['profile_id'])
                state.pop('portal_upload_ticket',None)
                state['last_document_id']=document['id']
                state['last_received_media']='document'
                self._save_state(owner,state)
                await self.workflow.offer(owner,chat_id,document_id=document['id'],ticket_id=ticket['id'],kind='replace')
                return
            selected=next((item['id'] for item in profiles if item['id']==state.get('active_profile')),None)
            if not selected and len(profiles)==1:
                selected=profiles[0]['id']
            document=self.billing.receive_document(owner,'unclassified',data,profile_id=None if explicit_csf else selected)
            state['last_document_id']=document['id']
            state['last_received_media']='document'
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
            if await self.workflow.guard(owner,chat_id,'td',pending['id']):
                return
            ticket=self.billing.complete_ticket_field(owner,pending['id'],pending['field'],text.split(maxsplit=1)[1],correction=pending.get('correction',False))
            state['ticket_input']=None
            self._save_state(owner,state)
            try:
                self.billing.browser_review(owner,ticket['id'])
            except AppError as error:
                if error.status!=404:
                    raise
                await self._ticket(owner,chat_id,ticket['id'])
            else:
                await self._browser(owner,chat_id,ticket['id'])
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
