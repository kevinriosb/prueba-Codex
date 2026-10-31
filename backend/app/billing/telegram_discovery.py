"""Un permiso inicial cubre leer el ticket y recorrer la web pública oficial."""
from ..errors import AppError


class TelegramDiscovery:
    def __init__(self,bridge):
        self.bridge,self.billing=bridge,bridge.billing

    def allow_public(self,owner,ticket,source):
        if self.billing.portal_permissions.route_allowed(owner,ticket):return
        review=self.billing.portal_permissions.request(owner,ticket['id'],route=True)
        self.billing.store.update('portal_permission',owner,review['id'],review|{'consent_source':source})
        self.billing.portal_permissions.decide(owner,review['id'],approve=True)

    async def open(self,owner,chat_id,ticket,source):
        tx=self.billing.portal_submission.current(owner,ticket['id'])
        if ticket.get('billing_url') and self.billing.browser.enabled and not ticket.get('delivery') and not (tx and tx.get('private')):
            self.allow_public(owner,ticket,source)
            try:
                await self.bridge._run_browser(owner,chat_id,ticket['id'])
                return
            except AppError as error:
                ticket=self.billing.resolve_ticket(owner,ticket['id'])
                self.billing.store.update('ticket',owner,ticket['id'],ticket|{'portal_discovery_error':error.message})
        await self.bridge._ticket(owner,chat_id,ticket['id'])

    async def after_analysis(self,owner,chat_id,ticket,document):
        from .receipt_quality import warnings
        for warning in warnings(ticket):await self.bridge.client.send(chat_id,warning)
        if ticket.get('billing_url_literal') and ticket.get('billing_url') and (ticket.get('portal_discovery') or {}).get('source')=='ticket' and not self.billing.portal_permissions.route_allowed(owner,ticket):
            await self.bridge._ticket(owner,chat_id,ticket['id'])
            return
        if (document.get('analysis_scope')=='portal-fields-v3' and document.get('analysis_consent')
                and self.billing.resolve_ticket(owner,document['ticket_id'])['id']==ticket['id']):
            await self.open(owner,chat_id,ticket,'ticket_analysis:'+document['id'])
        else:await self.bridge._ticket(owner,chat_id,ticket['id'])

    async def search(self,owner,chat_id,ticket_id):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        self.billing.portal_submission.guard(owner,ticket)
        await self.bridge.client.send(chat_id,'Buscando facturación en la web oficial…')
        ticket=await self.billing.portal_discovery.find(owner,ticket['id'],refresh=True)
        await self.open(owner,chat_id,ticket,'official_search_button')

    async def ask_site(self,owner,chat_id,ticket_id):
        state=self.bridge._state(owner)
        state.update(portal_site_input=ticket_id,ticket_input=None,portal_field_input=None)
        self.bridge._save_state(owner,state)
        await self.bridge.client.send(chat_id,'Escribe la web oficial del comercio. Buscaré su sección Facturación.')

    async def message(self,owner,chat_id,message):
        state=self.bridge._state(owner)
        identifier=state.get('portal_site_input');text=message.get('text','').strip()
        if not identifier or not text or text.startswith('/'):return False
        ticket=await self.billing.portal_discovery.supplied(owner,identifier,text)
        state.pop('portal_site_input',None);self.bridge._save_state(owner,state)
        await self.open(owner,chat_id,ticket,'official_site_supplied')
        return True
