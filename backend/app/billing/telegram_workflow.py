"""Menús y cambios de solicitud deterministas; nunca interpretados por el modelo."""
import base64
import re
import time

from ..errors import AppError
from .telegram_profiles import normalized


TERMINAL = {'FACTURACION_COMPLETADA'}
TICKET_ACTIONS = {'td','xe','xu','xr','ts','bd','bp','bn','ba','bs','bm','bo','ve','vc','ec','pr','tr','sa','sv','po','on','of'}
TICKET_ACTIONS.update({'lv','lr','lq','df','dw'})
REVIEW_ACTIONS = {'xa':'portal_data_review','xg':'portal_fill_review','xs':'portal_fill_review',
    'er':'email_approval','se':'email_approval','no':'email_approval','wa':'portal_permission','wx':'portal_permission'}
REVIEW_ACTIONS.update({'ls':'portal_send_review','lm':'portal_transaction','la':'portal_field_photo','lu':'portal_field_photo'})
TRANSIENT = ('last_ticket_id','browser_ticket_id','last_document_id','portal_upload_ticket','ticket_input','browser_choice',
    'expected_upload','replace_profile_id','profile_input','portal_field_input','portal_site_input','receipt_update_ticket')


class TelegramWorkflow:
    def __init__(self, bridge):
        self.bridge, self.billing = bridge, bridge.billing
        self.store = self.billing.store

    def state(self, owner):
        state = self.bridge._state(owner)
        if not state.get('workflow_version'):
            selected = state.get('last_ticket_id') or state.get('browser_ticket_id')
            try:
                ticket = self.billing.resolve_ticket(owner, selected) if selected else None
            except AppError as error:
                if error.status != 404:
                    raise
                ticket = None
            state.update(workflow_version=1, active_ticket_id=ticket['id'] if ticket and ticket['status'] not in TERMINAL else None,
                workflow_mode='active' if ticket and ticket['status'] not in TERMINAL else 'idle')
            self.bridge._save_state(owner, state)
        return state

    def active(self, owner):
        state = self.state(owner)
        identifier = state.get('active_ticket_id')
        if not identifier:
            return None
        try:
            ticket = self.billing.resolve_ticket(owner, identifier)
        except AppError as error:
            if error.status != 404:
                raise
            ticket = None
        if ticket and ticket['status'] not in TERMINAL and ticket.get('telegram_workflow') != 'cancelled':
            if ticket['id']!=identifier:
                self.activate(owner,ticket)
                ticket=self.billing.resolve_ticket(owner,ticket['id'])
            return ticket
        state.update(active_ticket_id=None, workflow_mode='idle')
        self.bridge._save_state(owner, state)
        return None

    def navigation(self, owner):
        return [('Menú principal', 'mn:' + self.state(owner)['id'] + ':home')]

    def label(self, ticket):
        reference = ticket.get('account_number') or ticket.get('folio') or ticket.get('reference_number') or 'sin referencia'
        return ticket['id'][:8] + ' · ' + (ticket.get('merchant') or 'Ticket')[:40] + ' · ' + str(reference)[:25]

    def stage(self, ticket):
        if ticket['status'] in TERMINAL:
            return 'Completada: factura recibida y validada.'
        if ticket.get('portal_delivery'):
            if ticket['portal_delivery']['state']=='unknown':return 'Envío al portal sin confirmar. No lo repitas.'
            if ticket.get('validation'):return 'Factura recibida y cotejada. Falta vigencia SAT.'
            return 'Solicitud enviada al portal. Esperando XML y PDF.'
        if ticket.get('delivery'):
            return 'Solicitud enviada. Esperando la factura.'
        if (ticket.get('portal_fill') or {}).get('state')=='complete':
            return 'Paso 4/4: formulario preparado, sin enviar.'
        if (ticket.get('portal_browser') or {}).get('fields'):
            return 'Paso 3/4: revisar datos del formulario.'
        if ticket['status']=='TICKET_RECIBIDO':
            return 'Paso 1/4: autorizar lectura del ticket.'
        if ticket.get('billing_url'):
            return 'Paso 2/4: encontrar el formulario.'
        if ticket.get('billing_email'):
            return 'Paso 3/4: revisar destinatario y correo.'
        return 'Paso 2/4: revisar cómo factura el comercio.'

    def activate(self, owner, ticket):
        state = self.state(owner)
        if state.get('active_ticket_id')!=ticket['id']:
            for key in TRANSIENT:
                state.pop(key, None)
        state.update(active_ticket_id=ticket['id'], last_ticket_id=ticket['id'], last_document_id=ticket['document_id'],
            workflow_mode='active', workflow_choice_id=None)
        ticket['telegram_workflow'] = 'active'
        self.store.update('ticket', owner, ticket['id'], ticket)
        self.bridge._save_state(owner, state)

    def awaiting(self, owner):
        state = self.state(owner)
        for key in TRANSIENT:
            state.pop(key, None)
        state.update(active_ticket_id=None, workflow_mode='awaiting_ticket', workflow_choice_id=None)
        self.bridge._save_state(owner, state)

    def pending(self, owner):
        state = self.state(owner)
        if not state.get('workflow_choice_id'):
            return None
        try:
            choice=self.store.get('workflow_choice', owner, state['workflow_choice_id'])
            if choice.get('ticket_id'):
                self.billing.resolve_ticket(owner,choice['ticket_id'])
            active=self.active(owner)
            if choice['_state']=='pending' and choice['from_ticket_id']==(active['id'] if active else None):
                return choice
            state=self.state(owner)
            state['workflow_choice_id']=None
            self.bridge._save_state(owner,state)
            return None
        except AppError as error:
            if error.status != 404:
                raise
            state['workflow_choice_id'] = None
            self.bridge._save_state(owner, state)
            return None

    def uncertain(self, owner, ticket):
        tx=self.billing.portal_submission.current(owner,ticket['id'])
        return bool(tx and tx['state']=='unknown') or any(item['ticket_id'] == ticket['id'] and item['_state'] in {'claimed','unknown'}
            for item in self.store.list('email_approval', owner))

    async def menu(self, owner, chat_id):
        pending = self.pending(owner)
        if pending:
            await self.prompt(owner, chat_id, pending)
            return
        state = self.state(owner)
        active = self.active(owner)
        profiles = self.store.list('profile', owner)
        text = ('Solicitud activa: ' + self.label(active)+'\n'+self.stage(active)) if active else ('Perfil listo. Envía tu ticket.' if profiles else 'Envía tu CSF una vez. Después, tus tickets.')
        if state.get('workflow_mode') == 'awaiting_ticket' and not active:
            text = 'Nueva solicitud. Envía una foto o PDF del nuevo ticket.'
        buttons = []
        if active:
            buttons.append(('Continuar solicitud activa', 'mn:' + state['id'] + ':continue'))
        buttons.extend([('Nueva solicitud', 'mn:' + state['id'] + ':new'),
            ('Mis solicitudes', 'mn:' + state['id'] + ':list'), ('Mi perfil fiscal', 'mn:' + state['id'] + ':profile')])
        if active:
            buttons.append(('Cerrar o cancelar solicitud', 'mn:' + state['id'] + ':stop'))
        await self.bridge.client.send(chat_id, text, buttons)

    async def offer(self, owner, chat_id, *, document_id=None, ticket_id=None, kind='switch'):
        active = self.active(owner)
        if document_id and kind=='switch':
            document=self.billing.document(owner,document_id)
            if document['purpose']=='unclassified':
                self.store.update('document',owner,document_id,document|{'intended_purpose':'ticket'})
        choice = self.store.add('workflow_choice', owner, {'from_ticket_id':active['id'] if active else None,
            'document_id':document_id, 'ticket_id':ticket_id, 'kind':kind}, state='pending', expires=time.time()+1800)
        state = self.state(owner)
        state['workflow_choice_id'] = choice['id']
        self.bridge._save_state(owner, state)
        await self.prompt(owner, chat_id, choice)

    async def prompt(self, owner, chat_id, choice):
        active = self.active(owner)
        text = 'Solicitud activa: ' + self.label(active) if active else 'Elige cómo continuar.'
        key = 'fw:' + choice['id'] + ':'
        if choice['kind'] == 'replace':
            text += '\nFoto recibida. ¿Es el mismo ticket u otro?'
            buttons = [('Otra foto del mismo ticket', key+'same'), ('Es otro ticket', key+'other'), ('Conservar la anterior', key+'keep')]
        elif choice['kind'] == 'resume':
            ticket = self.billing.resolve_ticket(owner, choice['ticket_id'])
            text = 'Retomar solicitud: ' + self.label(ticket)
            buttons = [('Retomar esta solicitud', key+'resume'), ('Volver al menú', key+'keep')]
        else:
            if choice.get('document_id'):
                text += '\nArchivo nuevo '+choice['document_id'][:8]+' guardado. Elige qué hacer con la anterior.'
            elif choice.get('ticket_id'):
                text += '\nElegiste: ' + self.label(self.billing.resolve_ticket(owner, choice['ticket_id']))
            else:
                text += '\n' + ('¿Cierras esta solicitud?' if choice['kind']=='stop' else '¿Cómo empiezas la nueva solicitud?')
            buttons = []
            next_label='retomar elegida' if choice.get('ticket_id') else 'abrir nueva'
            if choice['kind'] != 'stop':
                buttons.append(('Pausar anterior y '+next_label, key+'pause'))
            if active and not self.uncertain(owner, active):
                label = 'Detener seguimiento' if active.get('delivery') or active.get('portal_delivery') else 'Cancelar solicitud'
                buttons.append((label + (' y '+next_label if choice['kind']!='stop' else ''), key+'cancel'))
            if active and (active.get('delivery') or active.get('portal_delivery')):
                text += '\nLo ya enviado al comercio no se anula.'
            elif active and self.uncertain(owner, active):
                text += '\nEnvío sin confirmar: consulta su estado antes de cancelarlo.'
            buttons.append(('Continuar con la anterior', key+'keep'))
        await self.bridge.client.send(chat_id, text, buttons)

    async def new(self, owner, chat_id):
        pending=self.pending(owner)
        document_id=pending.get('document_id') if pending and pending.get('kind')=='switch' else None
        active=self.active(owner)
        if active:await self.close(owner,active,cancel=False)
        else:self.awaiting(owner)
        if document_id:
            await self.bridge._choose_document(owner,chat_id,document_id,'ticket',workflow_confirmed=True)
        else:await self.menu(owner, chat_id)

    async def admit(self, owner, chat_id, document_id):
        document = self.billing.document(owner, document_id)
        active = self.active(owner)
        target = self.billing.resolve_ticket(owner, document['ticket_id']) if document.get('ticket_id') else None
        if target and active and target['id']==active['id'] and self.pending(owner):
            await self.prompt(owner,chat_id,self.pending(owner))
            return False
        if active and (not target or target['id'] != active['id']):
            await self.close(owner,active,cancel=False)
            await self.bridge.client.send(chat_id,'Nuevo ticket activo. El anterior queda en Mis solicitudes.')
        if target:
            self.activate(owner, target)
        return True

    async def close(self, owner, ticket, *, cancel):
        if cancel:
            if self.uncertain(owner, ticket):
                raise AppError(409, 'workflow_uncertain', 'Envío sin confirmar. Revisa su estado; puedes pausarlo.')
            for kind in ('email_approval','portal_permission','portal_data_review','portal_fill_review','portal_send_review','portal_field_photo'):
                for review in self.store.list(kind, owner):
                    if review.get('ticket_id') == ticket['id'] and review['_state']=='pending':
                        self.store.update(kind, owner, review['id'], review, state='rejected')
            for grant in self.store.list('portal_grant', owner):
                if grant['ticket_id']==ticket['id'] and grant['_state']=='active':
                    self.store.update('portal_grant', owner, grant['id'], grant, state='revoked')
            if (ticket.get('tracking') or {}).get('enabled'):
                self.billing.configure_tracking(owner, ticket['id'], enabled=False, confirmed=True)
            await self.billing.sync_portal_permissions(owner, ticket['id'], cancel=True)
        ticket = self.billing.resolve_ticket(owner, ticket['id'])
        ticket['telegram_workflow'] = 'cancelled' if cancel else 'paused'
        self.store.update('ticket', owner, ticket['id'], ticket)
        self.store.audit(owner, ticket['id'], 'telegram_request_cancelled' if cancel else 'telegram_request_paused')
        self.awaiting(owner)

    async def choose(self, owner, chat_id, identifier, action):
        choice = self.store.get('workflow_choice', owner, identifier)
        state = self.state(owner)
        active = self.active(owner)
        if state.get('workflow_choice_id') != identifier or choice['_state'] != 'pending' or choice['from_ticket_id'] != (active['id'] if active else None):
            raise AppError(409, 'workflow_changed', 'El menú cambió. Abre /menu para continuar.')
        allowed = {'same','other','keep'} if choice['kind']=='replace' else {'resume','keep'} if choice['kind']=='resume' else {'cancel','keep'} if choice['kind']=='stop' else {'pause','cancel','keep'}
        if action not in allowed or action=='cancel' and (not active or self.uncertain(owner, active)):
            raise AppError(409, 'workflow_choice', 'Esa opción no está disponible. Abre /menu.')
        document = self.billing.document(owner, choice['document_id']) if choice.get('document_id') and action!='keep' else None
        target = self.billing.resolve_ticket(owner, choice['ticket_id']) if choice.get('ticket_id') and action!='keep' else None
        self.store.claim('workflow_choice', owner, identifier)
        state = self.state(owner)
        state['workflow_choice_id'] = None
        self.bridge._save_state(owner, state)
        if action == 'keep':
            if active:
                state = self.state(owner)
                state['last_document_id'] = active['document_id']
                self.bridge._save_state(owner, state)
                await self.bridge._ticket(owner, chat_id, active['id'])
            else:
                await self.menu(owner, chat_id)
            return
        if action == 'other':
            if active:await self.close(owner,active,cancel=False)
            await self.bridge._choose_document(owner,chat_id,document['id'],'ticket',workflow_confirmed=True)
            return
        if action == 'same':
            if not active or choice['ticket_id']!=active['id']:
                raise AppError(409,'workflow_changed','La solicitud cambió. Abre /menu.')
            if document['id']==active['document_id']:
                replacement=document
            else:
                replacement = self.billing.portal_preparation.replace_source(owner, active['id'], base64.b64decode(document['data']),
                    staged_id=document['id'] if document['purpose']=='unclassified' else None)
            self.activate(owner, self.billing.resolve_ticket(owner, active['id']))
            state = self.state(owner)
            state.pop('portal_upload_ticket', None)
            self.bridge._save_state(owner, state)
            await self.billing.portal_preparation.current(owner, replacement['ticket_id'])
            await self.bridge._browser(owner, chat_id, replacement['ticket_id'])
            return
        if active:
            await self.close(owner, active, cancel=action=='cancel')
            label='cancelada en el bot' if action=='cancel' else 'pausada'
            await self.bridge.client.send(chat_id,'Solicitud '+active['id'][:8]+' '+label+'.')
            if choice['kind']=='stop':
                state=self.state(owner)
                state['workflow_mode']='idle'
                self.bridge._save_state(owner,state)
        if target:
            self.activate(owner, target)
            await self.bridge._ticket(owner, chat_id, target['id'])
        elif document:
            await self.bridge._choose_document(owner, chat_id, document['id'], 'ticket', workflow_confirmed=True)
        else:
            await self.menu(owner, chat_id)

    async def callback(self, owner, chat_id, data):
        prefix, _, tail = data.partition(':')
        identifier, _, action = tail.partition(':')
        if prefix == 'mn':
            if identifier != self.state(owner)['id']:
                raise AppError(404, 'billing_not_found', 'Menú no encontrado.')
            if action=='new':
                await self.new(owner, chat_id)
            elif action=='list':
                await self.bridge.requests.list(owner, chat_id)
            elif action=='profile':
                await self.bridge.profiles.resume(owner, chat_id)
            elif action=='stop' and self.active(owner):
                await self.offer(owner, chat_id, kind='stop')
            elif action=='continue' and self.active(owner):
                if self.pending(owner):
                    await self.prompt(owner,chat_id,self.pending(owner))
                else:
                    await self.bridge._ticket(owner, chat_id, self.active(owner)['id'])
            else:
                await self.menu(owner, chat_id)
            return True
        if prefix=='fw':
            try:
                await self.choose(owner, chat_id, identifier, action)
            except AppError as error:
                if error.status!=404:
                    raise
                state=self.state(owner)
                if state.get('workflow_choice_id')==identifier:
                    state['workflow_choice_id']=None
                    self.bridge._save_state(owner,state)
                raise AppError(409,'workflow_changed','El archivo o menú venció. Abre /menu; tu perfil se conserva.') from None
            return True
        return False

    async def guard(self, owner, chat_id, prefix, identifier):
        ticket_id = None
        if prefix in TICKET_ACTIONS:
            ticket_id = self.billing.resolve_ticket(owner, identifier)['id']
        elif prefix in REVIEW_ACTIONS:
            try:
                ticket_id = self.store.get(REVIEW_ACTIONS[prefix], owner, identifier).get('ticket_id')
            except AppError as error:
                if prefix!='xs' or error.status!=404:
                    raise
                ticket_id=next((ticket['id'] for ticket in self.store.list('ticket',owner)
                    if (ticket.get('portal_fill') or {}).get('review_id')==identifier),None)
                if not ticket_id:
                    raise
        elif prefix in {'an','ra','dt'}:
            document = self.billing.document(owner, identifier)
            ticket_id = document.get('ticket_id')
        if not ticket_id:
            return False
        pending=self.pending(owner)
        if pending:
            active=self.active(owner)
            if prefix=='df' and active and active['id']==ticket_id:
                # Buscar en la tarjeta activa también expresa continuarla;
                # no volver a mostrar un menú de cambio/cierre abandonado.
                self.activate(owner,active)
            else:
                await self.prompt(owner,chat_id,pending)
                return True
        ticket = self.billing.resolve_ticket(owner, ticket_id)
        active = self.active(owner)
        if active and active['id'] != ticket['id']:
            await self.offer(owner, chat_id, ticket_id=ticket['id'])
            return True
        if not active:
            if ticket.get('telegram_workflow') in {'paused','cancelled'}:
                await self.offer(owner, chat_id, ticket_id=ticket['id'], kind='resume')
                return True
            if self.state(owner).get('workflow_mode')=='awaiting_ticket':
                await self.offer(owner, chat_id, ticket_id=ticket['id'], kind='resume')
                return True
            self.activate(owner, ticket)
        return False

    async def message(self, owner, chat_id, message):
        text = normalized(message.get('text',''))
        reply = message.get('reply_to_message') or {}
        attachment = message.get('document') or {}
        video = any(message.get(key) for key in ('video','animation','video_note')) or attachment.get('mime_type','').startswith('video/')
        reply_video = text in {'es este','es esta','este','esta'} and any(reply.get(key) for key in ('video','animation','video_note'))
        if video or reply_video or text in {'es este','es esta','este','esta'} and not reply and self.state(owner).get('last_received_media')=='video':
            state=self.state(owner)
            state['last_received_media']='video'
            self.bridge._save_state(owner,state)
            await self.bridge.client.send(chat_id, 'Recibí un video. Envía el ticket como foto o PDF.', self.navigation(owner))
            return True
        if text in {'es este','es esta','este','esta'} and (reply.get('photo') or reply.get('document')):
            await self.bridge._message(owner, chat_id, {key:value for key,value in reply.items() if key in {'photo','document','caption'}})
            return True
        if text in {'/start','/ayuda','/menu','/inicio','menu','inicio'}:
            await self.menu(owner, chat_id)
            return True
        if text in {'/nuevo','/nuevo_ticket','nueva solicitud','otra solicitud','nuevo ticket','otro ticket'} or re.search(r'\b(?:empieza|empezar|inicia|iniciar|comienza|comenzar)\b.*\b(?:(?:otro|nuevo) ticket|(?:otra|nueva) solicitud|de cero|de 0)\b', text):
            await self.new(owner, chat_id)
            return True
        if text in {'/cancelar','cancelar solicitud','cancela la solicitud'}:
            if self.active(owner):
                await self.offer(owner, chat_id, kind='stop')
            else:
                await self.menu(owner, chat_id)
            return True
        if text in {'/continuar','continuar solicitud'}:
            if self.active(owner):
                await self.bridge._ticket(owner, chat_id, self.active(owner)['id'])
            else:
                await self.menu(owner, chat_id)
            return True
        if text and not text.startswith('/') and self.pending(owner):
            await self.prompt(owner, chat_id, self.pending(owner))
            return True
        if text and not text.startswith('/') and self.state(owner).get('workflow_mode')=='awaiting_ticket':
            await self.menu(owner, chat_id)
            return True
        return False
