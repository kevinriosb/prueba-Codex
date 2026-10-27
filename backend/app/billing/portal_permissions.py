"""Permiso humano para leer portales: separado de análisis, perfil y facturación."""
import time

from ...browser_worker.policy import BrowserError,origin,secure_link
from ..errors import AppError


class PortalPermissionRequired(AppError):
    def __init__(self,origins):
        self.origins=origins
        super().__init__(403,'portal_permission','Autoriza el uso de este portal en Telegram antes de abrirlo.')


def portal_origin(url):
    try:
        return origin(secure_link(url))
    except (BrowserError,ValueError,TypeError):
        raise AppError(422,'portal_origin','El portal debe tener una dirección HTTPS pública válida.') from None


class PortalPermissions:
    def __init__(self,billing):
        self.billing,self.store=billing,billing.store

    def scope(self,ticket):
        return self.store.digest('portal-scope:'+ticket['id']+':'+ticket['profile_id']+':'+(ticket.get('billing_url') or ''))

    def allowed(self,owner,ticket):
        scope=self.scope(ticket)
        return sorted({item['origin'] for item in self.store.list('portal_grant',owner)
            if item['scope']==scope and item['_state']=='active'})

    def require(self,owner,ticket,target=None):
        requested=portal_origin(target or ticket.get('billing_url'))
        if requested not in self.allowed(owner,ticket):
            raise PortalPermissionRequired([requested])
        return requested

    def _expire_lookup(self,kind,owner,lookup):
        # Retirar solo metadatos vencidos de este permiso; no toca documentos.
        with self.store.database.connection(True) as conn:
            conn.execute('DELETE FROM billing_records WHERE kind=? AND owner=? AND lookup=? AND expires<=?',
                (kind,owner,lookup,time.time()))

    def candidates(self,owner,ticket):
        candidates={portal_origin(ticket.get('billing_url'))}
        record=self.store.find('browser_review',owner,self.store.digest('browser:'+ticket['id']))
        if record and record.get('scope')==self.scope(ticket):
            snapshot=record['snapshot']
            sources=list(snapshot['blocked_origins'])
            sources.extend(item['permission_origin'] for item in snapshot['actions'] if item.get('permission_origin'))
            for target in sources:
                try:
                    candidates.add(portal_origin(target))
                except AppError:
                    pass
        else:
            record=None
        return candidates,record

    def request(self,owner,ticket_id,*,origins=None,intent='browse'):
        self.billing.require_enabled()
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        if intent not in {'browse','details'}:
            raise AppError(422,'portal_permission','Tipo de revisión no válido.')
        targets=sorted({portal_origin(value) for value in (origins or [ticket.get('billing_url')])})
        candidates,record=self.candidates(owner,ticket)
        if not targets or len(targets)>12 or not set(targets)<=candidates:
            raise AppError(403,'portal_permission_source','Ese dominio no procede del ticket ni de la página observada.')
        scope=self.scope(ticket)
        lookup=self.store.digest('portal-review:'+scope+':'+intent+':'+','.join(targets))
        self._expire_lookup('portal_permission',owner,lookup)
        previous=self.store.find('portal_permission',owner,lookup)
        if previous and previous['_state']=='pending':
            return previous
        if previous:
            self.store.change_lookup('portal_permission',owner,previous['id'],self.store.digest('old-portal-review:'+previous['id']))
        root=portal_origin(ticket['billing_url'])
        expires=time.time()+900
        if any(target!=root for target in targets):
            if not record:
                raise AppError(409,'portal_permission_source','La referencia del portal venció. Revisa el ticket nuevamente.')
            expires=min(expires,record['_expires'])
        return self.store.add('portal_permission',owner,{'ticket_id':ticket['id'],'scope':scope,
            'origins':targets,'intent':intent,'purpose':'public_read'},lookup=lookup,state='pending',expires=expires)

    def decide(self,owner,identifier,*,approve):
        self.billing.require_enabled()
        review=self.store.get('portal_permission',owner,identifier)
        ticket=self.billing.resolve_ticket(owner,review['ticket_id'])
        candidates,_=self.candidates(owner,ticket)
        if approve and (review['scope']!=self.scope(ticket) or not set(review['origins'])<=candidates):
            raise AppError(409,'portal_permission_changed','El portal cambió. Revisa y autoriza nuevamente.')
        current=self.allowed(owner,ticket)
        if approve and len(set(current)|set(review['origins']))>12:
            raise AppError(409,'portal_permission_limit','Esta revisión alcanzó el máximo de dominios permitidos.')
        self.store.claim('portal_permission',owner,identifier)
        if approve:
            for target in review['origins']:
                lookup=self.store.digest('portal-grant:'+review['scope']+':'+target)
                self._expire_lookup('portal_grant',owner,lookup)
                previous=self.store.find('portal_grant',owner,lookup)
                if previous and previous['_state']=='active':
                    continue
                if previous:
                    self.store.delete('portal_grant',owner,previous['id'])
                self.store.add('portal_grant',owner,{'ticket_id':ticket['id'],'scope':review['scope'],
                    'origin':target,'purpose':'public_read'},lookup=lookup,expires=time.time()+1800)
            self.store.update('portal_permission',owner,identifier,review,state='approved')
            self.store.audit(owner,ticket['id'],'portal_public_permission_granted')
        else:
            for grant in self.store.list('portal_grant',owner):
                if grant['ticket_id']==ticket['id'] and grant['_state']=='active':
                    self.store.update('portal_grant',owner,grant['id'],grant,state='revoked')
            self.store.update('portal_permission',owner,identifier,review,state='rejected')
            self.store.audit(owner,ticket['id'],'portal_public_permission_cancelled')
        return review
