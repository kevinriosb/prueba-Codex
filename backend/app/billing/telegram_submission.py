"""Menús nativos para enviar un paso y corregir únicamente el campo solicitado."""
from datetime import datetime
import json
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
        if tx['state'] in {'unknown','submitted','expired','resolved','step_limit'}:
            text={'unknown':'Envío sin confirmar. No lo repitas.',
                'submitted':'Solicitud enviada al portal.',
                'expired':'La sesión venció. Revisa el portal antes de repetir.',
                'resolved':'Resuelto manualmente. No se afirma emisión ni validación SAT.',
                'step_limit':'Se alcanzó el límite de pasos del trámite. Necesita revisión humana.'}[tx['state']]
            if ticket.get('validation'):text+=' XML recibido y cotejado.'
            if ticket['status']=='FACTURACION_COMPLETADA':text='Factura lista. XML y PDF disponibles.'
            if tx.get('file_error'):text+='\n'+tx['file_error']
            if tx['state'] not in {'resolved','step_limit'}:buttons.append(('Consultar envío','lq:'+ticket['id']))
            if tx['state'] in {'unknown','expired'}:
                if tx.get('status_error')=='browser_session':text+=' La sesión del navegador venció; consultar no puede reconstruir la respuesta.'
                buttons.extend([('Reintentar desde el inicio','lx:'+ticket['id']),('Revisar mi correo','le:'+ticket['id']),('Marcar como resuelto manualmente','lz:'+ticket['id'])])
            for identifier in ticket.get('invoice_documents',[]):
                document=self.billing.document(owner,identifier)
                buttons.append(('Descargar '+('XML' if document['mime']=='application/xml' else 'PDF'),'dl:'+identifier))
            if ticket.get('validation') and not ticket['validation'].get('sat_verified'):buttons.append(('Validar vigencia SAT','sa:'+ticket['id']))
        else:
            approvals,missing,values=self.submission.approvals(owner,tx)
            text=('Paso llenado, con la red cerrada. Revisa de nuevo y confirma SOLO el envío de este paso.' if tx['state']=='prepared' else
                'Paso de navegación: revisa y autoriza únicamente este avance.' if tx['state']=='navigation' else 'Revisa y autoriza este paso del portal.')
            text+='\nPaso '+str(len(tx.get('steps',[]))+1)+' de un máximo de '+str(self.billing.settings.billing_portal_max_steps)+'.'
            if tx.get('stop_reason'):
                text+='\nLa navegación automática se detuvo por su límite.' if tx['stop_reason'] in {'time_limit','browser_steps'} else '\nEste control requiere tu autorización explícita; no se avanzó automáticamente.'
            if tx['view'].get('read_error'):text+='\n'+tx['view']['read_error']
            if missing:
                text='Falta: '+', '.join(f['label'] for f in missing)+'.'
                text+=' Escríbelo o envía una foto del campo.'
                if len(missing)==1 and missing[0].get('reference'):
                    state=self.bridge._state(owner)
                    state['portal_field_input']={'ticket_id':ticket['id'],'transaction_id':tx['id'],'reference':missing[0]['reference']}
                    self.bridge._save_state(owner,state)
                if any(f.get('key') in PROFILE_KEYS for f in missing):
                    text+=' Falta un dato del perfil: proporciónalo explícitamente; no se deducirá ni se inventará.'
                    buttons.append(('Mi perfil fiscal','mn:'+self.bridge._state(owner)['id']+':profile'))
                for item in missing:
                    if not item.get('reference') and item.get('key')=='total':
                        buttons.append(('Completar importe','td:'+ticket['id']+':total'))
                        state=self.bridge._state(owner)
                        state['ticket_input']={'id':ticket['id'],'field':'total'}
                        self.bridge._save_state(owner,state)
            if tx['view']['error']:text+='\nPortal: '+tx['view']['error']
            text+='\n'+'\n'.join(v['label']+': '+v['value'] for v in values)
            if ticket.get('total') and not any(value.get('key')=='total' for value in values):
                text+='\nImporte del ticket: '+str(ticket['total'])
            for field in tx['view']['snapshot']['fields']:
                if tx['state']!='prepared' and (field.get('key') not in PROFILE_KEYS or not self.billing.store.get('profile',owner,ticket['profile_id']).get(field.get('key'))):
                    buttons.append(('Completar '+field['label'][:30],'lm:'+tx['id']+':'+field['reference']))
            if tx.get('prior_values'):
                text+='\nEste paso puede reenviar datos ya autorizados en el portal:\n'+'\n'.join(k+': '+v for k,v in tx['prior_values'].items())
            for approval in approvals:
                text+='\nSe enviarán estos datos a: '+', '.join(approval['origins'])
                buttons.append((('Confirmar envío: ' if approval['stage']=='send' else 'Autorizar llenado: ')+approval['action']['label'][:30],('lc:' if approval['stage']=='send' else 'ls:')+approval['id']))
            if not approvals and not missing:text+='\nNo hay un botón de avance compatible. Requiere revisión manual.'
            buttons.append(('Actualizar este formulario','lr:'+ticket['id']))
        # Nunca aprobar una vista cortada por Telegram.
        if len(text)>3800:
            text='Los datos exceden la vista de Telegram. Revisa este formulario manualmente.'
            buttons=[b for b in buttons if not b[1].startswith(('ls:','lc:'))]
        await self.bridge.client.send(chat_id,'Solicitud '+ticket['id'][:8]+' · '+(ticket.get('merchant') or 'Ticket')[:45]+'\n'+text,buttons+self.bridge.workflow.navigation(owner))

    async def callback(self,owner,chat_id,prefix,identifier,reference=None):
        if prefix in {'lv','lr'}:await self.show(owner,chat_id,identifier,tx=await self.submission.review(owner,identifier,refresh=prefix=='lr'))
        elif prefix in {'ls','lc'}:
            self.clear_input(owner)
            tx=await (self.submission.prepare(owner,identifier) if prefix=='ls' else self.submission.send(owner,identifier))
            await self.show(owner,chat_id,tx['ticket_id'],tx=tx)
        elif prefix=='lq':
            tx=self.submission.current(owner,identifier)
            if not tx:await self.show(owner,chat_id,identifier);return
            before=json.dumps({k:v for k,v in tx.items() if k!='id' and not k.startswith('_')},sort_keys=True)
            tx=await self.submission.status(owner,tx)
            if before==json.dumps({k:v for k,v in tx.items() if k!='id' and not k.startswith('_')},sort_keys=True):
                await self.bridge.client.send(chat_id,'No hay cambios en el envío. La tarjeta anterior sigue vigente; no se repitió ningún envío.')
                return
            await self.show(owner,chat_id,identifier,tx=tx)
        elif prefix=='lx':
            review=self.submission.restart_request(owner,identifier)
            await self.bridge.client.send(chat_id,'Reiniciar puede duplicar una factura. Revisa primero tu correo y el portal. Confirma solo si aún NO se emitió; no se hará ningún envío sin nuevas aprobaciones.',
                [('Confirmo que no se emitió; reiniciar','ly:'+review['id'])])
        elif prefix=='ly':
            tx=await self.submission.restart(owner,identifier)
            await self.show(owner,chat_id,tx['ticket_id'],tx=tx)
        elif prefix=='le':
            ticket=self.billing.resolve_ticket(owner,identifier);self.submission.guard(owner,ticket)
            await self.bridge.client.send(chat_id,'Revisa el correo de entrega y spam para comprobar si llegó el XML/PDF. No se consultó tu buzón ni se volvió a solicitar la factura. Después puedes marcarla como resuelta manualmente.')
        elif prefix=='lz':
            tx=self.submission.resolve_manually(owner,identifier)
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
            if not photo.get('transaction_id'):
                ticket=self.receipt_value(owner,photo['ticket_id'],photo['key'],photo['value'])
                self.billing.store.update('portal_field_photo',owner,identifier,photo,state='used')
                self.clear_input(owner)
                await self.receipt_done(owner,chat_id,ticket)
                return
            tx=self.submission.set_field(owner,photo['transaction_id'],photo['reference'],photo['value'])
            self.billing.store.update('portal_field_photo',owner,identifier,photo,state='used')
            self.clear_input(owner)
            await self.show(owner,chat_id,tx['ticket_id'],tx=tx)

    def clear_input(self,owner):
        state=self.bridge._state(owner)
        state.pop('portal_field_input',None);state.pop('ticket_input',None)
        self.bridge._save_state(owner,state)

    def receipt_value(self,owner,ticket_id,key,value,correction=False):
        if key=='purchase_date' and '/' in value:
            try:value=datetime.strptime(value.strip(),'%d/%m/%Y').date().isoformat()
            except ValueError:raise AppError(422,'ticket_field','Escribe una fecha válida: DD/MM/AAAA.') from None
        return self.billing.complete_ticket_field(owner,ticket_id,key,value,correction=correction)

    async def receipt_done(self,owner,chat_id,ticket):
        tx=self.submission.current(owner,ticket['id'])
        if tx:
            await self.show(owner,chat_id,ticket['id'],tx=tx);return
        try:self.billing.browser_review(owner,ticket['id'])
        except AppError as error:
            if error.status!=404:raise
            await self.bridge._ticket(owner,chat_id,ticket['id'])
        else:await self.bridge._browser(owner,chat_id,ticket['id'])

    async def message(self,owner,chat_id,message):
        state=self.bridge._state(owner);pending=state.get('portal_field_input') or state.get('ticket_input')
        if not pending:return False
        if state.get('expected_upload')=='csf' or state.get('replace_profile_id'):
            self.clear_input(owner);return False
        text=message.get('text','').strip();caption=message.get('caption','').strip()
        if text.startswith('/') or caption.startswith('/'):return False
        active=self.bridge.workflow.active(owner)
        if not active or active['id']!=(pending.get('ticket_id') or pending.get('id')):
            self.clear_input(owner);return False
        if message.get('document') or message.get('photo'):
            attachment=message.get('document') or message['photo'][-1]
            data=await self.bridge.client.download(attachment['file_id'])
            photo=(self.submission.receive_crop(owner,pending['transaction_id'],pending['reference'],data) if pending.get('transaction_id') else
                self.submission.receive_receipt_crop(owner,pending['id'],pending['field'],data))
            await self.bridge.client.send(chat_id,'Leer solo este campo con OpenAI consume tokens.',[('Autorizar lectura del campo','la:'+photo['id'])])
        elif text:
            if not pending.get('transaction_id'):
                ticket=self.receipt_value(owner,pending['id'],pending['field'],text,pending.get('correction',False))
                self.clear_input(owner)
                await self.receipt_done(owner,chat_id,ticket)
                return True
            tx=self.submission.set_field(owner,pending['transaction_id'],pending['reference'],text)
            self.clear_input(owner)
            await self.show(owner,chat_id,tx['ticket_id'],tx=tx)
        else:return False
        return True
