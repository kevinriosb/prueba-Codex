"""Las fotos de corrección siguen la solicitud y no abren otro trámite."""
import base64
import re
import time

from ..errors import AppError
from .receipt_quality import match_photo,read_addresses

from .receipt import pending_receipt_fields
from .telegram_profiles import normalized


def receipt_intent(message):
    text=normalized(message.get('caption') or message.get('text',''))
    if re.search(r'\bno es (?:otro|un nuevo) ticket\b|\bmismo ticket\b|\bmisma solicitud\b',text):return 'same'
    if re.search(r'\b(?:otro|nuevo) ticket\b|\b(?:otra|nueva) solicitud\b',text):return 'new'
    if re.search(r'\b(?:foto|imagen|archivo|pdf|ticket)\b.{0,60}\b(?:nuevamente|de nuevo|otra vez|mas clar[oa]|corregid[oa])\b',text):return 'same'
    if re.search(r'\b(?:otra|nueva|mejor) (?:foto|imagen)\b|\b(?:vuelvo|volver) a (?:subir|enviar|mandar)\b',text):return 'same'
    return None


class TelegramReceipts:
    def __init__(self,bridge):
        self.bridge,self.billing=bridge,bridge.billing

    def candidates(self,owner,profile_id):
        return [t for t in self.billing.store.list('ticket',owner) if t.get('profile_id')==profile_id
            and not t.get('superseded_by') and t.get('telegram_workflow')!='cancelled'
            and t['status']!='FACTURACION_COMPLETADA' and not t.get('portal_manual_resolution')]

    async def stage(self,owner,chat_id,document,profile_id):
        candidates=self.candidates(owner,profile_id)
        if not candidates:return False
        active_id=(self.bridge.workflow.active(owner) or {}).get('id')
        lookup=self.billing.store.digest('photo-match:'+document['id']+profile_id+str(active_id))
        old=self.billing.store.find('receipt_photo_review',owner,lookup)
        if old and old['_state'] not in {'pending','read'}:
            await self.bridge.client.send(chat_id,'Esta foto ya tuvo una lectura. Usa la decisión pendiente o envía una foto más clara; no se repetirá el análisis.')
            return True
        review=old or self.billing.store.add('receipt_photo_review',owner,{'document_id':document['id'],
            'profile_id':profile_id,'candidate_ids':[t['id'] for t in candidates],'ticket_id':None,
            'from_ticket_id':(self.bridge.workflow.active(owner) or {}).get('id')},
            lookup=lookup,state='pending',expires=time.time()+1800)
        if old:candidates=[self.billing.resolve_ticket(owner,i) for i in review['candidate_ids']]
        state=self.bridge._state(owner);state['receipt_photo_review_id']=review['id']
        self.bridge._save_state(owner,state)
        await self.bridge.client.send(chat_id,'Antes de abrir otra solicitud, puedo comparar esta foto con tus tickets guardados. Leeré la foto y, después de observar el formulario, solo los campos que pida; buscaré facturación oficial si hace falta. Usa tokens de IA. Si se une a un ticket existente no consume otro análisis diario. Llenar y enviar seguirán requiriendo tus autorizaciones separadas.',
            ([('Autorizar lectura y comparar','fa:'+review['id'])] if review['_state']=='pending' else [])+
            [('Es el mismo ticket'+(' · '+self.bridge.workflow.label(t) if len(candidates)>1 else ''),'fm:'+review['id']+':'+str(i)) for i,t in enumerate(candidates)]+[('Es otro','fo:'+review['id'])])
        return True

    def quota_link(self,owner,document_id):
        with self.billing.database.connection(True) as conn:
            conn.execute("UPDATE billing_extractions SET status='linked_' || status WHERE owner=? AND document_id=? AND status NOT LIKE 'linked%'",(owner,document_id))

    async def merge(self,owner,chat_id,ticket,document):
        data=document.get('extracted') or {}
        tx=self.billing.portal_submission.current(owner,ticket['id'])
        # Un envío o llenado en curso no se altera por recibir una fotografía.
        if ticket.get('delivery') or ticket.get('portal_delivery') or tx and tx.get('private'):
            ticket.setdefault('photo_documents',[]).append(document['id'])
            self.billing.store.update('ticket',owner,ticket['id'],ticket)
            self.quota_link(owner,document['id'])
            self.billing.store.update('document',owner,document['id'],document|{'ticket_id':ticket['id'],'photo_linked':True})
            await self.bridge.client.send(chat_id,'Foto unida a la solicitud existente. El paso del portal en curso y sus aprobaciones se conservan; no se reenviaron datos.')
            return
        active=self.bridge.workflow.active(owner)
        if active and active['id']!=ticket['id']:await self.bridge.workflow.close(owner,active,cancel=False)
        if not data:
            self.quota_link(owner,document['id'])
            await self.correct(owner,chat_id,ticket,document)
            return
        data,qr=read_addresses(data,document)
        replacement=self.billing.portal_preparation.replace_source(owner,ticket['id'],base64.b64decode(document['data']),staged_id=document['id'])
        ticket=self.billing.resolve_ticket(owner,ticket['id'])
        if ticket['status'] in {'TICKET_RECIBIDO','ERROR'} and not ticket.get('is_ticket'):
            self.quota_link(owner,document['id'])
            result=await self.billing.analyze_ticket(owner,replacement['id'],consent=True)
            self.bridge.workflow.activate(owner,self.billing.resolve_ticket(owner,result['id']))
            self.clear_expectation(owner)
            await self.bridge.client.send(chat_id,'Foto unida a la solicitud pendiente; no se creó otro ticket ni se repitió la lectura.')
            await self.bridge.discovery.after_analysis(owner,chat_id,result,self.billing.document(owner,replacement['id']))
            return
        for key in ('merchant','issuer_rfc','store_number','purchase_date','purchase_time','folio','total','currency','subtotal','tax','tip','total_without_tip','total_with_tip'):
            if not ticket.get(key) and data.get(key):ticket[key]=data[key]
            elif data.get(key) and ticket.get(key) and str(data[key])!=str(ticket[key]):
                confidence={i['key']:i for i in ticket.get('field_confidence') or []}
                if key in {'merchant','store_number','purchase_date','purchase_time','folio','total'}:
                    confidence[key]={'key':key,'confidence':'low'};ticket['field_confidence']=list(confidence.values())
        if data.get('photo_issues') is not None:ticket['photo_issues']=data['photo_issues']
        confidences={i['key']:i for i in ticket.get('field_confidence') or []}
        for item in data.get('field_confidence') or []:
            if item['key'] not in confidences or data.get(item['key']) is not None and item['confidence'] in {'low','unreadable'}:
                confidences[item['key']]=item
        ticket['field_confidence']=list(confidences.values())
        from .receipt_quality import assess
        ticket=assess(ticket)
        if not ticket.get('billing_url') and data.get('billing_url'):
            ticket.update(portal_printed_url_candidate=data['billing_url'],billing_url_literal=data.get('billing_url_literal'),
                qr_billing_url=data.get('qr_billing_url'),qr_text_mismatch=data.get('qr_text_mismatch'),
                printed_addresses=data.get('printed_addresses'),billing_url_evidence=data.get('billing_url_evidence'))
        ticket.update(portal_source_pending=False,qr_urls=qr)
        self.billing.store.update('ticket',owner,ticket['id'],ticket)
        self.billing.store.update('document',owner,replacement['id'],replacement|{'photo_linked':True})
        self.quota_link(owner,document['id'])
        self.bridge.workflow.activate(owner,ticket)
        self.clear_expectation(owner)
        if ticket.get('portal_printed_url_candidate'):ticket=await self.billing.portal_discovery.find(owner,ticket['id'])
        await self.bridge.client.send(chat_id,'Foto unida a la misma solicitud. Conservé los datos anteriores; no abrí otro ticket ni consumí otro análisis diario.')
        await self.bridge._ticket(owner,chat_id,ticket['id'])

    async def decision(self,owner,chat_id,prefix,identifier,index=None):
        store=self.billing.store
        try:review=store.get('receipt_photo_review',owner,identifier)
        except AppError as error:
            if error.status!=404:raise
            raise AppError(409,'photo_decision','La foto o decisión venció. Usa la solicitud actual.') from None
        if review['_state'] not in {'pending','read'}:raise AppError(409,'photo_decision','Esta decisión ya fue utilizada.')
        state=self.bridge._state(owner)
        if (state.get('receipt_photo_review_id')!=identifier or state.get('workflow_choice_id')
                or (self.bridge.workflow.active(owner) or {}).get('id')!=review.get('from_ticket_id')):
            raise AppError(409,'photo_decision','El menú cambió. Usa la solicitud actual.')
        document=self.billing.document(owner,review['document_id'])
        candidates=[self.billing.resolve_ticket(owner,i) for i in review['candidate_ids']]
        if prefix=='fa':
            if review['_state']!='pending':raise AppError(409,'photo_decision','La foto ya fue leída; elige si es el mismo ticket u otro.')
            store.claim('receipt_photo_review',owner,identifier)
            document.update(profile_id=review['profile_id'],analysis_scope='portal-fields-v3')
            store.update('document',owner,document['id'],document)
            try:document,data=await self.billing._extract(owner,document['id'],'ticket',True,staged=True)
            except AppError as error:
                store.update('receipt_photo_review',owner,identifier,review,state='pending' if error.code=='billing_daily_limit' else 'read')
                if error.code!='billing_daily_limit':raise
                await self.bridge.client.send(chat_id,'Se alcanzó el tope: no analicé ni consumí un intento. Puedes confirmar si es uno de tus tickets existentes.',
                    [('Es el mismo ticket'+(' · '+self.bridge.workflow.label(t) if len(candidates)>1 else ''),'fm:'+identifier+':'+str(i)) for i,t in enumerate(candidates)]+[('Es otro','fo:'+identifier)])
                return
            if not data.get('is_ticket'):raise AppError(422,'photo_ticket','No se identificó como ticket; revisa el archivo.')
            document.update(photo_read=True)
            store.update('document',owner,document['id'],document)
            match,possible=match_photo(data,candidates)
            if match:
                store.update('receipt_photo_review',owner,identifier,review,state='complete')
                await self.merge(owner,chat_id,match,document)
                return
            review['candidate_ids']=[t['id'] for t in possible or candidates]
            store.update('receipt_photo_review',owner,identifier,review,state='read')
            await self.bridge.client.send(chat_id,'No hay coincidencia suficiente para unirla automáticamente. ¿Es el mismo ticket u otro?',
                [('Es el mismo ticket'+(' · '+t['id'][:8] if len(review['candidate_ids'])>1 else ''),'fm:'+identifier+':'+str(i)) for i,t in enumerate(possible or candidates)]+[('Es otro','fo:'+identifier)])
        elif prefix=='fm':
            try:
                selected=int(index or 0)
                if selected<0:raise ValueError()
                ticket=candidates[selected]
            except (IndexError,ValueError):raise AppError(422,'photo_decision','Elige una solicitud válida.') from None
            store.update('receipt_photo_review',owner,identifier,review,state='complete',version=review['_version'])
            await self.merge(owner,chat_id,ticket,document)
        else:
            store.update('receipt_photo_review',owner,identifier,review,state='complete',version=review['_version'])
            active=self.bridge.workflow.active(owner)
            if active:await self.bridge.workflow.close(owner,active,cancel=False)
            await self.bridge._choose_document(owner,chat_id,document['id'],'ticket',workflow_confirmed=True)

    def clear_expectation(self,owner):
        state=self.bridge._state(owner)
        for key in ('receipt_update_ticket','portal_upload_ticket','portal_site_input','ticket_input','portal_field_input'):
            state.pop(key,None)
        self.bridge._save_state(owner,state)

    async def before(self,owner,chat_id,message):
        attachment=message.get('document') or message.get('photo')
        intent=receipt_intent(message)
        active=self.bridge.workflow.active(owner)
        if attachment and intent=='new':
            if active:await self.bridge.workflow.close(owner,active,cancel=False)
            else:self.bridge.workflow.awaiting(owner)
        elif not attachment and intent=='same' and active:
            self.clear_expectation(owner)
            state=self.bridge._state(owner);state['receipt_update_ticket']=active['id']
            self.bridge._save_state(owner,state)
            await self.bridge.client.send(chat_id,'Envía la foto de este mismo ticket.')
            return True
        return False

    async def receive(self,owner,chat_id,message,data,*,explicit_csf=False):
        if explicit_csf:
            state=self.bridge._state(owner);state.pop('receipt_update_ticket',None)
            self.bridge._save_state(owner,state)
            return False
        if receipt_intent(message)=='new':return False
        ticket=self.bridge.workflow.active(owner)
        if not ticket:return False
        state=self.bridge._state(owner)
        expected=state.get('receipt_update_ticket')==ticket['id'] or state.get('portal_upload_ticket')==ticket['id']
        # Antes de la primera lectura aún no sabemos si faltan datos.
        partial=ticket['status']!='TICKET_RECIBIDO' and pending_receipt_fields(ticket)
        if not (expected or receipt_intent(message)=='same' or partial):
            received=self.billing.receive_document(owner,'unclassified',data,profile_id=ticket['profile_id'])
            document=self.billing.document(owner,received['id'])
            if document.get('ticket_id')==ticket['id']:
                await self.bridge.submission.receipt_done(owner,chat_id,ticket)
                return True
            return await self.stage(owner,chat_id,document,ticket['profile_id'])
        received=self.billing.receive_document(owner,'unclassified',data,profile_id=ticket['profile_id'])
        document=self.billing.document(owner,received['id'])
        if document.get('ticket_id') and document['ticket_id']!=ticket['id']:
            return False  # Un archivo conocido de otra solicitud conserva su identidad.
        if not document.get('ticket_id') and not (expected or receipt_intent(message)=='same'):
            return await self.stage(owner,chat_id,document,ticket['profile_id'])
        return await self.correct(owner,chat_id,ticket,document)

    async def correct(self,owner,chat_id,ticket,document):
        self.bridge.workflow.activate(owner,ticket)
        if document['id']==ticket['document_id']:
            self.clear_expectation(owner)
            await self.bridge.submission.receipt_done(owner,chat_id,ticket)
            return True
        replacement=self.billing.portal_preparation.replace_source(owner,ticket['id'],
            base64.b64decode(document['data']),staged_id=document['id'])
        self.clear_expectation(owner)
        state=self.bridge._state(owner)
        state.update(last_document_id=replacement['id'],last_received_media='document')
        self.bridge._save_state(owner,state)
        ticket=self.billing.resolve_ticket(owner,ticket['id'])
        if not self.billing.portal_preparation.replacement_pending(owner,ticket):
            await self.bridge.submission.receipt_done(owner,chat_id,ticket)
            return True
        review=self.billing.portal_preparation.receipt_update_request(owner,ticket['id'])
        search=not ticket.get('billing_url') and not (ticket.get('billing_email') and ticket.get('email_explicitly_for_billing'))
        text='Foto actualizada; misma solicitud.\nLeerla'+(' y buscar facturación' if search else '')+' con IA usa saldo de OpenAI.'
        await self.bridge.client.send(chat_id,text,[('Leer foto actualizada'+(' y buscar facturación' if search else ''),'xa:'+review['id'])]+self.bridge.workflow.navigation(owner))
        return True

    async def after_read(self,owner,chat_id,review):
        ticket=self.billing.resolve_ticket(owner,review['ticket_id'])
        if not ticket.get('billing_url') and not (ticket.get('billing_email') and ticket.get('email_explicitly_for_billing')):
            await self.bridge.discovery.search(owner,chat_id,ticket['id'])
        else:await self.bridge.submission.receipt_done(owner,chat_id,ticket)
