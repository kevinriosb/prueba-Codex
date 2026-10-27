"""Adaptador de conversación: reutiliza el Runner, límites e historial existentes."""
import json

from ..errors import AppError


class TelegramAgent:
    def __init__(self, service, bridge):
        self.service, self.bridge = service, bridge

    def conversation(self, owner, *, new=False):
        state = self.bridge._state(owner)
        identifier = state.get('conversation_id')
        if identifier and not new:
            # La propiedad se comprueba incluso si el estado se corrompe.
            return self.service.public_conversation(identifier, owner)
        self.service.database.ensure_telegram_user(owner)
        conversation = self.service.database.create_conversation(owner, self.service.settings.max_conversations)
        state['conversation_id'] = conversation['id']
        self.bridge._save_state(owner, state)
        return self.service.public_conversation(conversation['id'], owner)

    async def send_text(self, chat_id, text):
        text = str(text)
        if len(text) > 15_200:
            text = text[:15_100] + '\n[Respuesta recortada por el límite del canal.]'
        for start in range(0, len(text), 3800):
            await self.bridge.client.send(chat_id, text[start:start + 3800])

    async def history(self, owner, chat_id):
        conversation = self.conversation(owner)
        messages = conversation['messages'][-8:]
        if not messages:
            await self.bridge.client.send(chat_id, 'Todavía no hay mensajes. Puedes escribir tu primera petición.')
        for message in messages:
            label = 'Tú' if message['role'] == 'user' else ('Aviso' if message['role'] == 'error' else 'Asistente')
            await self.send_text(chat_id, label + ': ' + message['content'])
        if conversation['approval']:
            await self.show_approval(chat_id,conversation['approval'])

    async def conversations(self, owner, chat_id):
        rows=self.service.database.list_conversations(owner)
        if not rows:
            await self.bridge.client.send(chat_id,'Todavía no tienes conversaciones. Puedes escribir tu primera petición.')
            return
        await self.bridge.client.send(chat_id,'Elige una conversación propia para continuar:',
            [('Conversación '+str(index+1)+' · '+row['id'][:8],'go:'+row['id']) for index,row in enumerate(rows)])

    async def open(self, owner, chat_id, identifier):
        self.service.public_conversation(identifier,owner)
        state=self.bridge._state(owner)
        state['conversation_id']=identifier
        self.bridge._save_state(owner,state)
        await self.history(owner,chat_id)

    async def delete(self, owner, chat_id, identifier):
        self.service.database.delete_conversation(identifier,owner)
        state=self.bridge._state(owner)
        if state.get('conversation_id')==identifier:
            state['conversation_id']=None
            self.bridge._save_state(owner,state)
        await self.bridge.client.send(chat_id,'Conversación eliminada. Tus perfiles y tickets se conservan.')

    async def reply(self, owner, chat_id, *, text=None, approval_id=None, approve=None):
        conversation = self.conversation(owner)
        result = await self.service.execute(conversation['id'], owner, text=text,
            approval_id=approval_id, approve=approve, channel='telegram',channel_context=self.bridge.agent_context(owner))
        message = result['messages'][-1]
        reviews = [event['review'] for event in message['events'] if isinstance(event.get('review'), dict)]
        # Una observación seguida de un clic reemplaza la vista anterior del portal.
        browser_reviews=[review for review in reviews if review.get('kind')=='browser']
        if browser_reviews:
            reviews=[review for review in reviews if review.get('kind')!='browser']+[browser_reviews[-1]]
        if not reviews and not result['approval']:
            await self.send_text(chat_id, message['content'])
        # Solo eventos creados por herramientas del servidor; nunca JSON del modelo.
        for review in reviews:
            await self.bridge.show_review(owner, chat_id, review)
        if result['approval']:
            await self.show_approval(chat_id,result['approval'])

    async def show_approval(self,chat_id,approval):
        preview = json.dumps(approval['tools'], ensure_ascii=False, indent=2)
        if len(preview) > 3000:
            await self.bridge.client.send(chat_id,
                'La acción supera el tamaño que podemos revisar aquí. Recházala y pide una acción más pequeña.',
                [('Rechazar acción', 'gr:' + approval['id'])])
        else:
            await self.bridge.client.send(chat_id, 'Revisa la acción antes de autorizarla:\n' + preview,
                [('Aprobar esta acción', 'ga:' + approval['id']), ('Rechazar acción', 'gr:' + approval['id'])])

    async def decide(self, owner, chat_id, approval_id, approve):
        conversation = self.conversation(owner)
        pending = conversation['approval']
        if not pending or pending['id'] != approval_id:
            raise AppError(409, 'invalid_approval', 'La confirmación caducó o ya fue utilizada.')
        await self.reply(owner, chat_id, approval_id=approval_id, approve=approve)
