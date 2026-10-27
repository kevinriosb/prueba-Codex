"""Consulta de solicitudes y presupuestos propios sin recurrir al modelo."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..errors import AppError


STATUS_LABELS = {
    'TICKET_RECIBIDO': 'Pendiente de análisis', 'DATOS_EXTRAIDOS': 'Datos listos',
    'PENDIENTE_CONFIRMACION': 'Pendiente de tu confirmación', 'CORREO_PREPARADO': 'Borrador preparado',
    'CORREO_ENVIADO': 'Correo enviado', 'ESPERANDO_RESPUESTA': 'Esperando al comercio',
    'FACTURA_RECIBIDA': 'Factura recibida', 'FACTURA_VALIDADA': 'Factura validada',
    'FACTURACION_COMPLETADA': 'Completada', 'REQUIERE_INTERVENCION': 'Requiere revisión', 'ERROR': 'Análisis pendiente de resolver',
}
REQUEST_TEXTS = {
    '/estado', 'repetir solicitud', 'repetir la solicitud', 'repite la solicitud',
    'reintentar solicitud', 'ver solicitud', 'ver mi solicitud', 'estado de mi solicitud',
    'estado del ticket', 'ya esta en tramite', 'donde reviso', 'donde lo reviso',
}


class TelegramRequests:
    def __init__(self, bridge):
        self.bridge, self.billing, self.settings = bridge, bridge.billing, bridge.settings

    def tickets(self, owner):
        return [item for item in self.billing.store.list('ticket', owner) if not item.get('superseded_by')]

    def summary(self, owner, ticket, *, analysis=None):
        approvals = [item for item in self.billing.store.list('email_approval', owner)
            if item['ticket_id'] == ticket['id']]
        uncertain = any(item['_state'] in {'claimed', 'unknown'} for item in approvals)
        pending = next((item for item in approvals if item['_state'] == 'pending'), None)
        text = 'Estado: ' + STATUS_LABELS.get(ticket['status'], 'Requiere revisión') + '.'
        if uncertain:
            return text + '\nEnvío incierto. Revisa Gmail; no lo repitas.', None, False
        if ticket.get('delivery'):
            return text + '\nSolicitud enviada; consulta la respuesta abajo.', None, False
        if pending:
            return text + '\nAún no se envió. Revisa el borrador abajo.', pending['id'], False
        if analysis:
            text='Estado: '+analysis['label']+'.'
        text += '\nNo se ha enviado una solicitud al comercio.'
        return text, None, True

    def analysis_status(self, owner, ticket, document):
        """Un rechazo de admisión antiguo no representa la cuota actual.

        Solo presenta el estado: no borra errores históricos, reservas ni intentos,
        y nunca inicia el análisis. Un intento previo sigue bloqueado tras el reset.
        """
        if ticket['status']!='ERROR' or (ticket.get('analysis_error') or {}).get('code')!='billing_daily_limit' or ticket.get('delivery'):
            return None
        if document is None:
            return {'label':'Archivo temporal vencido','available':False,
                'message':'Envía otra copia del ticket. Tu perfil se conserva.'}
        with self.billing.database.connection() as conn:
            attempted=conn.execute('SELECT 1 FROM billing_extractions WHERE owner=? AND document_id=?',
                (owner,document['id'])).fetchone()
        if attempted or document.get('analysis_consent'):
            return {'label':'Análisis ya intentado','available':False,
                'message':'Este archivo ya tuvo un intento. No se repetirá automáticamente.'}
        usage=self.usage(owner)
        if usage['analyses']>=self.settings.billing_max_daily_extractions:
            return {'label':'En espera de cupo','available':False,
                'message':f"Análisis: {usage['analyses']}/{self.settings.billing_max_daily_extractions}. Se restablece {usage['reset']} ({self.settings.server_timezone})."}
        return {'label':'Pendiente de análisis','available':True,
            'message':'Hay cupo disponible. Pulsa Autorizar análisis.'}

    async def show(self, owner, chat_id):
        state = self.bridge._state(owner)
        identifier = state.get('last_ticket_id')
        if identifier:
            try:
                await self.bridge._ticket(owner, chat_id, identifier)
                return
            except AppError as error:
                if error.status != 404:
                    raise
        tickets = self.tickets(owner)
        if len(tickets) == 1:
            await self.bridge._ticket(owner, chat_id, tickets[0]['id'])
        elif tickets:
            await self.list(owner, chat_id)
        else:
            await self.bridge.client.send(chat_id, 'No hay tickets guardados. Envía uno aquí.')

    async def list(self, owner, chat_id):
        tickets = self.tickets(owner)
        if not tickets:
            await self.bridge.client.send(chat_id, 'No hay tickets guardados. Envía uno aquí.')
            return
        # Mantener el detalle directo cuando solo hay una solicitud.
        if len(tickets) == 1:
            await self.bridge._ticket(owner, chat_id, tickets[0]['id'])
            return
        buttons = []
        for item in tickets[:20]:
            reference = item.get('account_number') or item.get('folio') or item.get('reference_number') or item['id'][:8]
            label = (item.get('merchant') or 'Ticket')[:22] + ' · ' + str(reference)[:16]
            label += ' · ' + STATUS_LABELS.get(item['status'], 'Revisar')
            buttons.append((label[:90], 'ts:' + item['id']))
        await self.bridge.client.send(chat_id, 'Tus tickets. Elige uno para ver su estado:', buttons)

    def usage(self, owner):
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.billing.database.connection() as conn:
            analyses = conn.execute('SELECT COUNT(*) FROM billing_extractions WHERE owner=? AND day=?',
                (owner, today.date().isoformat())).fetchone()[0]
            updates = conn.execute("SELECT COUNT(*) FROM billing_records WHERE kind='channel_update' AND owner=? AND created>=?",
                (owner, today.timestamp())).fetchone()[0]
        documents=self.billing.store.list('document',owner)
        expiries=[item['_expires'] for item in documents if item['_expires']]
        return self.billing.database.usage(owner) | {'analyses': analyses, 'updates': updates,
            'reset': (today + timedelta(days=1)).astimezone(ZoneInfo(self.settings.server_timezone)).strftime('%d/%m %H:%M'),
            'documents': len(documents),'next_file_expiry':min(expiries) if expiries else None}

    async def limits(self, owner, chat_id, *, code=None):
        usage = self.usage(owner)
        rows = {
            'billing_daily_limit': f"Análisis de archivos: {usage['analyses']}/{self.settings.billing_max_daily_extractions} hoy.",
            'daily_limit': f"Chat IA: {usage['runs']}/{self.settings.max_daily_runs} hoy; reserva de tokens {usage['tokens_accounted']}/{self.settings.max_daily_tokens}.",
            'telegram_daily_limit': f"Mensajes y acciones: {usage['updates']}/{self.settings.billing_max_daily_telegram_updates} hoy.",
        }
        text = ('Alcanzaste el límite diario.\n' + rows[code]) if code in rows else '\n'.join(rows.values())
        text += '\nSe restablece: ' + usage['reset'] + ' (' + self.settings.server_timezone + ').'
        if code is None:
            text += f"\nArchivos activos: {usage['documents']}/{self.settings.billing_max_documents}."
        text += '\n/estado y /tickets no usan IA. Los intentos fallidos de análisis también cuentan.'
        await self.bridge.client.send(chat_id, text)

    async def error(self, owner, chat_id, error):
        if error.code in {'billing_daily_limit', 'daily_limit'}:
            await self.limits(owner, chat_id, code=error.code)
        elif error.code in {'turn_limit', 'context_limit', 'execution_interrupted'}:
            await self.bridge.client.send(chat_id,
                'Este chat llegó a su límite o se interrumpió. /nuevo abre otro y conserva tu perfil y tickets. /estado muestra tu solicitud.')
        elif error.code == 'document_limit':
            usage = self.usage(owner)
            expiry=''
            if usage['next_file_expiry']:
                expiry=' Primer vencimiento: '+datetime.fromtimestamp(usage['next_file_expiry'],ZoneInfo(self.settings.server_timezone)).strftime('%d/%m %H:%M')+'.'
            await self.bridge.client.send(chat_id,
                f"Archivos guardados: {usage['documents']}/{self.settings.billing_max_documents}. Es un límite de espacio, no de IA.{expiry} /documentos · /estado. No hace falta reiniciar tu perfil.")
        elif error.code in {'duplicate_ticket', 'already_requested', 'pending_email_approval', 'mail_delivery_unknown'}:
            await self.show(owner, chat_id)
        else:
            await self.bridge.client.send(chat_id, error.message)
