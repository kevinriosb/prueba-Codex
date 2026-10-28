"""Enlaces públicos confirmados por persona; nunca conserva sesiones del navegador."""
import re
import time
import unicodedata
from urllib.parse import unquote

from ...browser_worker.policy import bookmark_url,origin
from ..errors import AppError


FORM_KEYS={'rfc','account_number','folio','reference_number','billing_code','web_id','fiscal_regime','cfdi_use'}


def brand_words(value):
    value=''.join(char for char in unicodedata.normalize('NFKD',value or '').casefold()
        if not unicodedata.combining(char)).translate(str.maketrans('','','\'’´ʼ'))
    words=re.findall(r'[a-z0-9]+',value)
    return [word for word in words if word not in {'logo','logotipo','marca','brand','factura','facturacion','electronica','portal'}]


def brand_key(value):
    return ''.join(brand_words(value))


def same_brand_label(left,right):
    first,second=brand_key(left),brand_key(right)
    if first==second:
        return True
    # Comparar palabras completas del nombre, nunca prefijos de otra marca.
    def contains(words,expected):
        return len(expected)>=4 and any(''.join(words[start:end])==expected
            for start in range(len(words)) for end in range(start+1,len(words)+1))
    return contains(brand_words(left),second) or contains(brand_words(right),first)


def has_billing_form(snapshot):
    return any(item.get('key') in FORM_KEYS for item in snapshot['fields'])


def brand_options(snapshot):
    options=[item for item in snapshot['actions'] if item.get('brand') and brand_key(item.get('label')) not in
        {'','facebook','instagram','twitter','x','youtube','linkedin','tiktok','inicio','home'}]
    # Un logo aislado (o repetido en la cabecera/pie) no es un selector de marcas.
    return options if len({brand_key(item['label']) for item in options})>=2 else []


def matching_brand(ticket,snapshot,*,label=None):
    if has_billing_form(snapshot):
        return None
    expected=brand_key(label or ticket.get('merchant'))
    if len(expected)<3:
        return None
    if not label and not brand_options(snapshot):
        return None
    candidates=[item for item in snapshot['actions'] if item.get('label') and brand_key(item['label'])==expected]
    if not candidates:
        candidates=[item for item in snapshot['actions'] if item.get('label') and same_brand_label(label or ticket.get('merchant'),item['label'])]
    return candidates[0] if len(candidates)==1 else None


class PortalMemory:
    def __init__(self,billing):
        self.billing,self.store=billing,billing.store

    def key(self,ticket):
        if len(brand_key(ticket.get('merchant')))<3 or not ticket.get('billing_url'):
            return None
        return self.store.digest('portal-memory:'+brand_key(ticket['merchant'])+':'+
            self.store.digest((ticket.get('issuer_rfc') or '').upper())+':'+origin(self._source(ticket)))

    def _source(self,ticket):
        from .portal_permissions import portal_origin
        return portal_origin(ticket['billing_url'])

    def suggestion(self,owner,ticket):
        lookup=self.key(ticket)
        record=self.store.find('portal_memory',owner,lookup) if lookup else None
        return record if record and bookmark_url(record['url'])==record['url'] else None

    def entry_url(self,owner,ticket):
        selected=ticket.get('selected_portal')
        if selected:
            record=self.suggestion(owner,ticket)
            if record and selected=={'id':record['id'],'url':record['url']}:
                return record['url']
        return ticket.get('billing_url')

    def select(self,owner,ticket_id,*,remembered=True):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        previous=ticket.get('selected_portal')
        if remembered:
            record=self.suggestion(owner,ticket)
            if not record:
                raise AppError(404,'portal_memory','No hay un portal guardado vigente para este comercio.')
            ticket['selected_portal']={'id':record['id'],'url':record['url']}
        else:
            ticket.pop('selected_portal',None)
        if previous!=ticket.get('selected_portal'):
            for key in ('portal_inspection','portal_browser','portal_browser_error'):
                ticket.pop(key,None)
        return self.store.update('ticket',owner,ticket['id'],ticket)

    def propose(self,owner,ticket_id,observation_id=None):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        record=self.billing.browser_review(owner,ticket['id'])
        snapshot=record['snapshot']
        if observation_id is not None and observation_id!=snapshot['observation_id']:
            raise AppError(409,'browser_stale','La página cambió. Revisa el portal antes de guardarlo.')
        if snapshot['network_blocked'] or snapshot['captcha'] or snapshot.get('login_required') or not has_billing_form(snapshot):
            raise AppError(409,'portal_memory_form','Primero llega al formulario de facturación y revisa que corresponda al comercio.')
        candidates=[item.get('source_url') for item in snapshot['fields'] if item.get('key') in FORM_KEYS]
        urls=list(dict.fromkeys(value for value in candidates if value and bookmark_url(value)==value))
        if not candidates or not all(candidates) or len(urls)!=1 or not ticket.get('merchant'):
            raise AppError(409,'portal_memory_url','No se encontró un enlace público estable para guardar.')
        url=urls[0]
        if origin(url) not in self.billing.portal_permissions.allowed(owner,ticket) and not self.billing.portal_permissions.route_allowed(owner,ticket):
            raise AppError(403,'portal_memory_permission','Autoriza el dominio del formulario antes de guardarlo.')
        # Los valores de la compra no se convierten en una liga reutilizable.
        decoded=unquote(url).casefold()
        for name in ('folio','account_number','reference_number','billing_code','web_id'):
            value=str(ticket.get(name) or '').strip().casefold()
            if len(value)>=5 and value in decoded:
                raise AppError(409,'portal_memory_url','El enlace contiene una referencia de compra; no se guardará para otros tickets.')
        merchant=record.get('brand_label') or ticket['merchant']
        if same_brand_label(merchant,ticket['merchant']):
            merchant=ticket['merchant']
        lookup=self.key(ticket|{'merchant':merchant})
        if not lookup:
            raise AppError(409,'portal_memory_merchant','No se identificó una marca clara para guardar el enlace.')
        return {'ticket_id':ticket['id'],'merchant':merchant,'url':url,
            'source':self._source(ticket),'lookup':lookup,'observation_id':snapshot['observation_id']}

    def save(self,owner,ticket_id,observation_id):
        proposal=self.propose(owner,ticket_id,observation_id)
        lookup=proposal.pop('lookup')
        previous=self.store.find('portal_memory',owner,lookup)
        if previous and previous['url']==proposal['url']:
            return previous
        if previous:
            self.store.delete('portal_memory',owner,previous['id'])
        if self.store.count('portal_memory',owner)>=50:
            raise AppError(409,'portal_memory_limit','Ya hay 50 portales guardados. Olvida uno con /portales.')
        self.billing.portal_permissions._expire_lookup('portal_memory',owner,lookup)
        result=self.store.add('portal_memory',owner,proposal,lookup=lookup,expires=time.time()+30*86400)
        self.store.audit(owner,ticket_id,'public_portal_remembered')
        return result

    def forget(self,owner,identifier):
        self.store.delete('portal_memory',owner,identifier)
