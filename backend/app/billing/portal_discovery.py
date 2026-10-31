"""Encuentra una web oficial pública; nunca usa un QR de encuesta como portal."""
import time
import asyncio
from datetime import datetime,timezone
from urllib.parse import urlsplit

from ...browser_worker.policy import secure_link,bookmark_url,origin,BrowserError
from ..errors import AppError
from .receipt import portal_url,unrelated_billing_link,printed_billing_addresses
from .portal_probe import probe_url


class PortalDiscovery:
    def __init__(self,billing):
        self.billing,self.store=billing,billing.store
        self.probe=probe_url

    def diagnostic(self,owner,ticket,url,purpose,result):
        from .receipt_quality import public_address
        purpose=purpose if purpose in {'billing','official','other','unclear'} else 'unclear'
        result=result if result in {'valid','no_response','blocked','survey_filtered','not_billing','not_literal','not_checked'} else 'blocked'
        item={'url':public_address(url),'purpose':purpose,'result':result}
        items=ticket.setdefault('printed_address_diagnostics',[])
        items[:]=[i for i in items if i['url']!=item['url']]
        items.append(item)
        self.billing.portal_diagnostic(owner,ticket,tool='printed_address',phase='response_check',
            address={'url':url,'purpose':purpose,'result':result})

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

    async def check_response(self,url):
        url=self.canonical(url)
        if not url:return {'url':None,'reachable':False,'reason':'invalid_url','status':None,'checked_at':time.time()}
        # No sigue redirecciones ni observa el formulario antes del permiso.
        try:
            async with asyncio.timeout(5):result=await self.probe(url)
        except TimeoutError:result={'url':url,'reachable':False,'reason':'timeout','status':None}
        return result|{'checked_at':time.time()}

    async def checked(self,owner,ticket,url,source,evidence):
        printed_url=portal_url(url) if source=='ticket' else None
        result=await self.check_response(url)
        if source=='ticket':
            self.diagnostic(owner,ticket,url,'billing','valid' if result['reachable'] else
                'blocked' if result['reason'] in {'private_address','invalid_url','browser_address'} else 'no_response')
        url=result['url']
        if not result['reachable']:
            ticket['portal_last_entry_failure']=result
            ticket.setdefault('portal_rejected_addresses',[]).append({'url':url,'reason':result['reason']})
            self.store.update('ticket',owner,ticket['id'],ticket)
            return None
        ticket['portal_entry_check']=result
        ticket.update(billing_url=printed_url or url,portal_driven=True,portal_stage='awaiting_form',
            portal_discovery={'source':source,'url':url,'evidence':str(evidence or '')[:500]},portal_discovery_error=None)
        return self.store.update('ticket',owner,ticket['id'],ticket)

    async def find(self,owner,ticket_id,*,refresh=False):
        self.billing.require_enabled()
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        ticket=self.repair(owner,ticket)
        if not ticket.get('printed_addresses') and not ticket.get('legacy_printed_address') and not ticket.get('billing_url') and not ticket.get('portal_printed_url_candidate'):
            ticket['printed_address_note']='La lectura no transcribió una dirección de facturación. No significa que el ticket no la tenga.'
        printed,rejected=printed_billing_addresses(ticket.get('printed_addresses'))
        for item in ticket.get('printed_addresses') or ([ticket['legacy_printed_address']] if ticket.get('legacy_printed_address') else []):
            if not item.get('url'):continue
            reason=next((r['reason'] for r in rejected if r['url']==(portal_url(item['url']) or item['url'])),None)
            result=('survey_filtered' if unrelated_billing_link(item['url'],item.get('evidence')) else
                'not_billing' if item['purpose']!='billing' else 'not_literal' if reason=='address_not_printed' else
                'blocked' if reason else 'not_checked')
            self.diagnostic(owner,ticket,item['url'],item['purpose'],result)
        if ticket.get('qr_billing_url'):
            printed=[{'url':ticket['qr_billing_url'],'evidence':'QR de facturación decodificado localmente'},
                *[i for i in printed if i['url']!=ticket['qr_billing_url']]]
        ticket['portal_rejected_addresses']=list({(item['url'],item['reason']):item
            for item in [*ticket.get('portal_rejected_addresses',[]),*rejected]}.values())
        candidate=ticket.get('billing_url') or ticket.get('portal_printed_url_candidate')
        if candidate and not printed:
            printed=[{'url':candidate,'evidence':ticket.get('billing_url_evidence') or ''}]
        # Comprobaciones acotadas; nunca reintentar una dirección fallida en esta búsqueda.
        attempted=set()
        async def attempt(url,source,evidence):
            canonical=self.canonical(url)
            if not canonical or canonical in attempted or len(attempted)>=4:return None
            attempted.add(canonical)
            return await self.checked(owner,ticket,url,source,evidence)
        for item in printed[:2]:
            found=await attempt(item['url'],'ticket',item['evidence'])
            if found:return found
        ticket.pop('portal_printed_url_candidate',None)
        ticket.update(billing_url=None,portal_stage='awaiting_official_site')
        self.store.update('ticket',owner,ticket['id'],ticket)
        if any(i['subject']=='billing_address' for i in ticket.get('photo_issues') or []):
            from .receipt_quality import warnings
            return self.store.update('ticket',owner,ticket['id'],ticket|{'portal_discovery_error':' '.join(warnings(ticket)),
                'portal_stage':'awaiting_clear_photo'})
        for saved in self.billing.portal_memory.for_merchant(owner,ticket)[:1]:
            found=await attempt(saved['url'],'saved',saved.get('merchant'))
            if found:return found
        if not ticket.get('merchant'):
            return self.store.update('ticket',owner,ticket['id'],ticket|{'portal_discovery_error':'No leí el comercio. Escríbelo para buscar su web oficial.'})
        printed=self.canonical(ticket.get('merchant_website'))
        evidence=ticket.get('merchant_website_evidence') or ''
        if printed and urlsplit(printed).hostname.removeprefix('www.') in evidence.casefold():
            found=await attempt(printed,'printed',evidence)
            if found:return found
        if len(attempted)>=4:
            return self.store.update('ticket',owner,ticket['id'],ticket|{'portal_discovery_error':
                'Estas direcciones no respondieron. Escribe la web oficial del comercio para continuar.'})
        lookup=self.store.digest('official-site:'+ticket['merchant'].strip().casefold())
        old=self.store.find('portal_discovery',owner,lookup)
        if old and (not refresh or old.get('url')):
            if old.get('url'):
                found=await attempt(old['url'],'web_search',old.get('evidence'))
                if found:return found
            return self.store.update('ticket',owner,ticket['id'],ticket|{'portal_discovery_error':old.get('message') or 'No encontré una web oficial verificable. Escríbela.'})
        if not hasattr(self.billing.extractor,'discover_official_site'):
            return self.store.update('ticket',owner,ticket['id'],ticket|{'portal_discovery_error':'Escribe la web oficial del comercio para buscar Facturación.'})
        day=datetime.now(timezone.utc).date().isoformat()
        used=sum(record.get('day')==day for record in self.store.list('portal_discovery',owner))
        if used>=self.billing.settings.billing_max_daily_extractions:
            return self.store.update('ticket',owner,ticket['id'],ticket|{'portal_discovery_error':'Alcanzaste el límite de búsquedas. Puedes escribir la web oficial.'})
        if self.billing.analysis_lock.locked():raise AppError(429,'billing_busy','Hay otra lectura en curso. Espera unos segundos.')
        async with self.billing.analysis_lock:
            if old:
                # El botón solicita otro intento real. Conservar el anterior
                # para que los fallos también cuenten en la cuota diaria.
                self.store.change_lookup('portal_discovery',owner,old['id'],self.store.digest('previous-official:'+old['id']))
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
        if record.get('url'):
            found=await attempt(record['url'],'web_search',record.get('evidence'))
            if found:return found
            record['message']='La dirección encontrada no respondió. Escribe la web oficial del comercio.'
        return self.store.update('ticket',owner,ticket['id'],ticket|{'portal_discovery_error':record['message']})

    async def supplied(self,owner,ticket_id,value):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        self.billing.portal_submission.guard(owner,ticket)
        if ticket.get('portal_delivery'):raise AppError(409,'already_requested','Consulta el envío existente antes de cambiar el portal.')
        url=self.canonical(value)
        if not url:raise AppError(422,'official_site','Escribe una web pública del comercio; una encuesta no sirve para facturar.')
        found=await self.checked(owner,ticket,url,'user',url)
        if found:return found
        raise AppError(422,'official_site','Esa dirección no respondió. Escribe otra web oficial del comercio.')
