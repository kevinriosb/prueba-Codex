"""Permiso humano para leer portales: separado de análisis, perfil y facturación."""
import time
import json

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
        selected=json.dumps(ticket.get('selected_portal'),sort_keys=True) if ticket.get('selected_portal') else ''
        return self.store.digest('portal-scope:'+ticket['id']+':'+ticket['profile_id']+':'+(ticket.get('billing_url') or '')+selected)

    def allowed(self,owner,ticket):
        scope=self.scope(ticket)
        return sorted({item['origin'] for item in self.store.list('portal_grant',owner)
            if item['scope']==scope and item['_state']=='active'})

    def require(self,owner,ticket,target=None):
        requested=portal_origin(target or self.billing.portal_memory.entry_url(owner,ticket))
        if requested not in self.allowed(owner,ticket) and not (target and self.route_allowed(owner,ticket)):
            raise PortalPermissionRequired([requested])
        return requested

    def route_allowed(self,owner,ticket):
        # Solo el consentimiento nuevo y explícito cubre destinos/recursos del
        # recorrido público. Los permisos antiguos por dominio no se amplían.
        scope=self.scope(ticket)
        return any(item.get('route_version')==1 and item['scope']==scope and item['_state']=='active'
            for item in self.store.list('portal_grant',owner))

    def _expire_lookup(self,kind,owner,lookup):
        # Retirar solo metadatos vencidos de este permiso; no toca documentos.
        with self.store.database.connection(True) as conn:
            conn.execute('DELETE FROM billing_records WHERE kind=? AND owner=? AND lookup=? AND expires<=?',
                (kind,owner,lookup,time.time()))

    def candidates(self,owner,ticket):
        candidates={portal_origin(ticket.get('billing_url')),portal_origin(self.billing.portal_memory.entry_url(owner,ticket))}
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

    def request(self,owner,ticket_id,*,origins=None,intent='browse',route=False):
        self.billing.require_enabled()
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        if intent not in {'browse','details'}:
            raise AppError(422,'portal_permission','Tipo de revisión no válido.')
        targets=sorted({portal_origin(value) for value in (origins or [self.billing.portal_memory.entry_url(owner,ticket)])})
        root=portal_origin(self.billing.portal_memory.entry_url(owner,ticket))
        if route and (intent!='browse' or targets!=[root]):
            raise AppError(422,'portal_permission','El recorrido debe empezar en el portal del ticket.')
        candidates,record=self.candidates(owner,ticket)
        if not targets or len(targets)>12 or not set(targets)<=candidates:
            raise AppError(403,'portal_permission_source','Ese dominio no procede del ticket ni de la página observada.')
        scope=self.scope(ticket)
        lookup=self.store.digest('portal-review:'+scope+':'+intent+':'+','.join(targets)+(':route-v1' if route else ''))
        self._expire_lookup('portal_permission',owner,lookup)
        previous=self.store.find('portal_permission',owner,lookup)
        if previous and previous['_state']=='pending':
            return previous
        if previous:
            self.store.change_lookup('portal_permission',owner,previous['id'],self.store.digest('old-portal-review:'+previous['id']))
        expires=time.time()+900
        if any(target!=root for target in targets):
            if not record:
                raise AppError(409,'portal_permission_source','La referencia del portal venció. Revisa el ticket nuevamente.')
            expires=min(expires,record['_expires'])
        return self.store.add('portal_permission',owner,{'ticket_id':ticket['id'],'scope':scope,
            'origins':targets,'intent':intent,'purpose':'public_read',**({'route_version':1} if route else {})},lookup=lookup,state='pending',expires=expires)

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
                if previous and previous['_state']=='active' and (not review.get('route_version') or previous.get('route_version')==1):
                    continue
                if previous:
                    self.store.delete('portal_grant',owner,previous['id'])
                self.store.add('portal_grant',owner,{'ticket_id':ticket['id'],'scope':review['scope'],
                    'origin':target,'purpose':'public_read',**({'route_version':1} if review.get('route_version')==1 else {})},lookup=lookup,expires=time.time()+1800)
            self.store.update('portal_permission',owner,identifier,review,state='approved')
            self.store.audit(owner,ticket['id'],'portal_public_permission_granted')
        else:
            for grant in self.store.list('portal_grant',owner):
                if grant['ticket_id']==ticket['id'] and grant['_state']=='active':
                    self.store.update('portal_grant',owner,grant['id'],grant,state='revoked')
            self.store.update('portal_permission',owner,identifier,review,state='rejected')
            self.store.audit(owner,ticket['id'],'portal_public_permission_cancelled')
        return review
