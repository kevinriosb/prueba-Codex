"""Menús nativos para enviar un paso y corregir únicamente el campo solicitado."""
from ..errors import AppError
from .portal_preparation import PROFILE_KEYS


class TelegramSubmission:
    def __init__(self,bridge):
        self.bridge,self.billing=bridge,bridge.billing
        self.submission=getattr(self.billing,'portal_submission',None)

    async def show(self,owner,chat_id,ticket_id,*,tx=None):
        tx=tx or await self.submission.review(owner,ticket_id)
        ticket=self.billing.resolve_ticket(owner,tx['ticket_id'])
        buttons=[]
        if tx['state'] in {'unknown','submitted','expired'}:
            text={'unknown':'Envío sin confirmar. No lo repitas.',
                'submitted':'Solicitud enviada al portal.',
                'expired':'La sesión venció. Revisa el portal antes de repetir.'}[tx['state']]
            if ticket.get('validation'):text+=' XML recibido y cotejado.'
            if ticket['status']=='FACTURACION_COMPLETADA':text='Factura lista. XML y PDF disponibles.'
            if tx.get('file_error'):text+='\n'+tx['file_error']
            buttons.append(('Consultar envío','lq:'+ticket['id']))
            for identifier in ticket.get('invoice_documents',[]):
                document=self.billing.document(owner,identifier)
                buttons.append(('Descargar '+('XML' if document['mime']=='application/xml' else 'PDF'),'dl:'+identifier))
            if ticket.get('validation') and not ticket['validation'].get('sat_verified'):buttons.append(('Validar vigencia SAT','sa:'+ticket['id']))
        else:
            approvals,missing,values=self.submission.approvals(owner,tx)
            text='Revisa y autoriza este paso del portal.'
            if tx['view']['error']:text+='\nPortal: '+tx['view']['error']
            if missing:
                text='Falta: '+', '.join(f['label'] for f in missing)+'.'
                text+=' Escríbelo o envía una foto del campo.'
                if len(missing)==1 and missing[0].get('reference') and missing[0].get('key') not in PROFILE_KEYS:
                    state=self.bridge._state(owner)
                    state['portal_field_input']={'ticket_id':ticket['id'],'transaction_id':tx['id'],'reference':missing[0]['reference']}
                    self.bridge._save_state(owner,state)
                if any(f.get('key') in PROFILE_KEYS for f in missing):
                    text+=' Actualiza y confirma el dato fiscal en Mi perfil fiscal.'
                    buttons.append(('Mi perfil fiscal','mn:'+self.bridge._state(owner)['id']+':profile'))
            text+='\n'+'\n'.join(v['label']+': '+v['value'] for v in values)
            for field in tx['view']['snapshot']['fields']:
                if field.get('key') not in PROFILE_KEYS:
                    buttons.append(('Completar '+field['label'][:30],'lm:'+tx['id']+':'+field['reference']))
            for approval in approvals:
                text+='\nSe enviarán estos datos a: '+', '.join(approval['origins'])
                buttons.append(('Confirmar: '+approval['action']['label'][:35],'ls:'+approval['id']))
            if not approvals and not missing:text+='\nNo hay un botón de avance compatible. Requiere revisión manual.'
            buttons.append(('Actualizar este formulario','lr:'+ticket['id']))
        # Nunca aprobar una vista cortada por Telegram.
        if len(text)>3800:
            text='Los datos exceden la vista de Telegram. Revisa este formulario manualmente.'
            buttons=[b for b in buttons if not b[1].startswith('ls:')]
        await self.bridge.client.send(chat_id,'Solicitud '+ticket['id'][:8]+' · '+(ticket.get('merchant') or 'Ticket')[:45]+'\n'+text,buttons+self.bridge.workflow.navigation(owner))

    async def callback(self,owner,chat_id,prefix,identifier,reference=None):
        if prefix in {'lv','lr'}:await self.show(owner,chat_id,identifier)
        elif prefix=='ls':
            self.clear_input(owner)
            tx=await self.submission.send(owner,identifier)
            await self.show(owner,chat_id,tx['ticket_id'],tx=tx)
        elif prefix=='lq':
            tx=self.submission.current(owner,identifier)
            if not tx:await self.show(owner,chat_id,identifier);return
            tx=await self.submission.status(owner,tx)
            await self.show(owner,chat_id,identifier,tx=tx)
        elif prefix=='lm':
            tx=self.billing.store.get('portal_transaction',owner,identifier)
            field=next((f for f in tx['view']['snapshot']['fields'] if f['reference']==reference),None)
            if not field:raise AppError(409,'portal_field','El campo cambió. Actualiza el paso.')
            state=self.bridge._state(owner)
            state['portal_field_input']={'ticket_id':tx['ticket_id'],'transaction_id':identifier,'reference':reference}
            self.bridge._save_state(owner,state)
            text=field['label']+': escribe el dato o envía una foto de ese campo.'
            if field.get('options'):text+='\nOpciones: '+'; '.join(field['options'])
            await self.bridge.client.send(chat_id,text)
        elif prefix=='la':
            photo=await self.submission.read_crop(owner,identifier)
            text=('Leí '+photo['label']+': '+photo['value']) if photo.get('value') else 'No se lee ese campo. Escribe el dato o envía otra foto.'
            await self.bridge.client.send(chat_id,text,[('Usar dato leído','lu:'+identifier)] if photo.get('value') else None)
        elif prefix=='lu':
            photo=self.billing.store.get('portal_field_photo',owner,identifier)
            if photo['_state']!='read' or not photo.get('value'):raise AppError(409,'portal_field','Primero autoriza la lectura del campo.')
            tx=self.submission.set_field(owner,photo['transaction_id'],photo['reference'],photo['value'])
            self.billing.store.update('portal_field_photo',owner,identifier,photo,state='used')
            self.clear_input(owner)
            await self.show(owner,chat_id,tx['ticket_id'],tx=tx)

    def clear_input(self,owner):
        state=self.bridge._state(owner);state.pop('portal_field_input',None);self.bridge._save_state(owner,state)

    async def message(self,owner,chat_id,message):
        state=self.bridge._state(owner);pending=state.get('portal_field_input')
        if not pending:return False
        if state.get('expected_upload')=='csf' or state.get('replace_profile_id'):
            self.clear_input(owner);return False
        text=message.get('text','').strip();caption=message.get('caption','').strip()
        if text.startswith('/') or caption.startswith('/'):return False
        active=self.bridge.workflow.active(owner)
        if not active or active['id']!=pending['ticket_id']:
            self.clear_input(owner);return False
        if message.get('document') or message.get('photo'):
            attachment=message.get('document') or message['photo'][-1]
            data=await self.bridge.client.download(attachment['file_id'])
            photo=self.submission.receive_crop(owner,pending['transaction_id'],pending['reference'],data)
            await self.bridge.client.send(chat_id,'Leer solo este campo con OpenAI consume tokens.',[('Autorizar lectura del campo','la:'+photo['id'])])
        elif text:
            tx=self.submission.set_field(owner,pending['transaction_id'],pending['reference'],text)
            self.clear_input(owner)
            await self.show(owner,chat_id,tx['ticket_id'],tx=tx)
        else:return False
        return True
