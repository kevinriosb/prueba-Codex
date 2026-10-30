"""Las fotos de corrección siguen la solicitud y no abren otro trámite."""
import base64
import re

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
        if not (expected or receipt_intent(message)=='same' or partial):return False
        received=self.billing.receive_document(owner,'unclassified',data,profile_id=ticket['profile_id'])
        document=self.billing.document(owner,received['id'])
        if document.get('ticket_id') and document['ticket_id']!=ticket['id']:
            return False  # Un archivo conocido de otra solicitud conserva su identidad.
        if not document.get('ticket_id') and not (expected or receipt_intent(message)=='same'):
            await self.bridge.workflow.offer(owner,chat_id,document_id=document['id'],ticket_id=ticket['id'],kind='receipt')
            return True
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
