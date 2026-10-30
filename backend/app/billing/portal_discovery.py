"""Encuentra una web oficial pública; nunca usa un QR de encuesta como portal."""
import time
from datetime import datetime,timezone
from urllib.parse import urlsplit

from ...browser_worker.policy import secure_link,bookmark_url,origin,BrowserError
from ..errors import AppError
from .receipt import portal_url,unrelated_billing_link


class PortalDiscovery:
    def __init__(self,billing):
        self.billing,self.store=billing,billing.store

    def canonical(self,value):
        url=portal_url(value)
        if not url or unrelated_billing_link(url):return None
        try:
            url=secure_link(url)
            return bookmark_url(url) or origin(url)+'/'
        except BrowserError:return None

    def repair(self,owner,ticket):
        tx=self.billing.portal_submission.current(owner,ticket['id'])
        if (not ticket.get('delivery') and not ticket.get('portal_delivery') and not (tx and tx.get('private'))
                and ticket.get('billing_url') and unrelated_billing_link(ticket['billing_url'],ticket.get('billing_url_evidence'))):
            for key in ('selected_portal','portal_checkpoint','portal_form_checkpoint','portal_browser','portal_inspection'):
                ticket.pop(key,None)
            ticket.update(billing_url=None,portal_stage='awaiting_official_site',portal_discovery_error='El QR es de encuesta. Buscaré la web oficial para facturar.')
            ticket=self.store.update('ticket',owner,ticket['id'],ticket)
            self.store.audit(owner,ticket['id'],'survey_link_ignored')
        return ticket

    def assign(self,owner,ticket,url,source,evidence):
        ticket.update(billing_url=url,portal_driven=True,portal_stage='awaiting_form',
            portal_discovery={'source':source,'url':url,'evidence':str(evidence or '')[:500]},portal_discovery_error=None)
        return self.store.update('ticket',owner,ticket['id'],ticket)

    async def find(self,owner,ticket_id):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        ticket=self.repair(owner,ticket)
        if ticket.get('billing_url') or ticket.get('email_explicitly_for_billing') and ticket.get('billing_email'):return ticket
        if not ticket.get('merchant'):
            return self.store.update('ticket',owner,ticket['id'],ticket|{'portal_discovery_error':'No leí el comercio. Escríbelo para buscar su web oficial.'})
        printed=self.canonical(ticket.get('merchant_website'))
        evidence=ticket.get('merchant_website_evidence') or ''
        if printed and urlsplit(printed).hostname.removeprefix('www.') in evidence.casefold():
            return self.assign(owner,ticket,printed,'printed',evidence)
        lookup=self.store.digest('official-site:'+ticket['merchant'].strip().casefold())
        old=self.store.find('portal_discovery',owner,lookup)
        if old:
            if old.get('url'):return self.assign(owner,ticket,old['url'],'web_search',old.get('evidence'))
            return self.store.update('ticket',owner,ticket['id'],ticket|{'portal_discovery_error':old.get('message') or 'No encontré una web oficial verificable. Escríbela.'})
        if not hasattr(self.billing.extractor,'discover_official_site'):
            return self.store.update('ticket',owner,ticket['id'],ticket|{'portal_discovery_error':'Escribe la web oficial del comercio para buscar Facturación.'})
        day=datetime.now(timezone.utc).date().isoformat()
        used=sum(record.get('day')==day for record in self.store.list('portal_discovery',owner))
        if used>=self.billing.settings.billing_max_daily_extractions:
            return self.store.update('ticket',owner,ticket['id'],ticket|{'portal_discovery_error':'Alcanzaste el límite de búsquedas. Puedes escribir la web oficial.'})
        if self.billing.analysis_lock.locked():raise AppError(429,'billing_busy','Hay otra lectura en curso. Espera unos segundos.')
        async with self.billing.analysis_lock:
            record=self.store.add('portal_discovery',owner,{'day':day,'merchant':ticket['merchant']},lookup=lookup,
                state='pending',expires=time.time()+86400)
            self.store.claim('portal_discovery',owner,record['id'])
            try:
                found=await self.billing.extractor.discover_official_site(ticket['merchant'])
                url=self.canonical(found.get('url'))
                record.update(url=url,evidence=found.get('evidence'),message=None if url else 'No encontré una web oficial verificable. Escríbela.')
                self.store.update('portal_discovery',owner,record['id'],record,state='complete')
            except Exception:
                record.update(url=None,message='No se completó la búsqueda oficial. Escribe la web del comercio.')
                self.store.update('portal_discovery',owner,record['id'],record,state='failed')
        ticket=self.billing.resolve_ticket(owner,ticket['id'])
        if record.get('url'):return self.assign(owner,ticket,record['url'],'web_search',record.get('evidence'))
        return self.store.update('ticket',owner,ticket['id'],ticket|{'portal_discovery_error':record['message']})

    def supplied(self,owner,ticket_id,value):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        self.billing.portal_submission.guard(owner,ticket)
        if ticket.get('portal_delivery'):raise AppError(409,'already_requested','Consulta el envío existente antes de cambiar el portal.')
        url=self.canonical(value)
        if not url:raise AppError(422,'official_site','Escribe una web pública del comercio; una encuesta no sirve para facturar.')
        return self.assign(owner,ticket,url,'user',url)
