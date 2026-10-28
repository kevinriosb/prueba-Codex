"""El formulario determina la lectura del ticket y una revisión humana precede al llenado."""
import base64
import json
import re
import sqlite3
import time
from datetime import datetime,timezone
from decimal import Decimal

from ..errors import AppError
from .models import TicketExtraction,Status
from .portal_memory import public_form_ready
from .portal_navigation import next_public_action
from .receipt import missing_receipt_fields,purchase_reference


RECEIPT_KEYS={'folio','account_number','reference_number','billing_code','web_id','store_number',
    'toll_station','lane','purchase_date','purchase_time','total','issuer_rfc'}
PROFILE_KEYS={'rfc','name','postal_code','fiscal_regime','cfdi_use','email'}
IDENTITY_KEYS={'merchant','issuer_rfc','purchase_date','folio','total','currency'}


def form_shape(snapshot):
    return {'page':snapshot.get('bookmark_url') or snapshot['page'],
        'fields':[{key:item.get(key) for key in ('key','label','kind','input_type','required','editable','max_length','placeholder','options','source_url')}
            for item in snapshot['fields']],'unsupported':snapshot.get('unsupported_required',[])}


def portal_value(field,value):
    if value is None:
        return None
    value=str(value).strip()
    if field.get('key')=='purchase_date' and field.get('input_type','text')!='date':
        hint=(field['label']+' '+field.get('placeholder','')).casefold()
        try:
            parsed=datetime.strptime(value,'%Y-%m-%d')
            if re.search(r'dd\s*[/.-]\s*mm\s*[/.-]\s*(?:aaaa|yyyy)',hint):
                separator='/' if '/' in hint else '-' if '-' in hint else '.'
                value=parsed.strftime('%d'+separator+'%m'+separator+'%Y')
        except ValueError:
            return None
    return value or None


class PortalPreparation:
    def __init__(self,billing):
        self.billing=billing
        self.store=billing.store

    def shape_key(self,snapshot):
        return self.store.digest(json.dumps(form_shape(snapshot),ensure_ascii=False,sort_keys=True))

    async def current(self,owner,ticket_id,*,refresh=False):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        self.billing.portal_permissions.require(owner,ticket)
        try:
            previous=self.billing.browser_review(owner,ticket['id'])
            if not refresh:return previous
            try:return await self.billing.browse_portal(owner,ticket['id'])
            except AppError as error:
                if error.code!='browser_private':raise
                await self.billing.browser.close_session(owner,previous['snapshot']['session_id'])
                self.store.delete('browser_review',owner,previous['id'])
        except AppError as error:
            if error.status!=404:raise
        record=await self.billing.browse_portal(owner,ticket['id'])
        for _ in range(7):
            if public_form_ready(record['snapshot']):break
            action=next_public_action(ticket,record['snapshot'])
            if not action:break
            record=await self.billing.browse_portal(owner,ticket['id'],observation_id=record['snapshot']['observation_id'],reference=action['reference'])
        return record

    def receipt_fields(self,snapshot):
        return [item for item in snapshot['fields'] if item.get('key') in RECEIPT_KEYS]

    def source(self,owner,ticket):
        try:
            document=self.billing.document(owner,ticket['document_id'])
        except AppError as error:
            if error.status!=404:raise
            raise AppError(410,'portal_source_expired','La foto temporal venció. Reenvía solo este ticket para leer sus campos; el perfil se conserva.') from None
        if document['purpose']!='ticket' or document.get('profile_id')!=ticket['profile_id']:
            raise AppError(409,'portal_source','El archivo no corresponde al perfil de este ticket.')
        return document

    def analysis_request(self,owner,ticket_id,snapshot):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        document=self.source(owner,ticket)
        fields=self.receipt_fields(snapshot)
        signature=self.shape_key(snapshot)
        # Una lectura fallida o parcial no debe ofrecer otro botón que cobre
        # el mismo archivo y los mismos campos. Se completa el dato o reenvía foto.
        attempted=self.store.digest('portal-data:'+ticket['id']+document['sha256']+signature)
        if self.store.find('portal_data',owner,attempted):return None
        lookup=self.store.digest('portal-data-review:'+ticket['id']+document['id']+signature)
        existing=self.store.find('portal_data_review',owner,lookup)
        if existing and existing['_state']=='pending':return existing
        if existing:return None
        return self.store.add('portal_data_review',owner,{'ticket_id':ticket['id'],'document_id':document['id'],
            'sha256':document['sha256'],'signature':signature,'scope':self.billing.portal_permissions.scope(ticket),
            'fields':[{'key':item['key'],'label':item['label']} for item in fields]},
            lookup=lookup,state='pending',expires=time.time()+self.billing.settings.approval_ttl_seconds)

    async def read_approved(self,owner,identifier):
        review=self.store.get('portal_data_review',owner,identifier)
        ticket=self.billing.resolve_ticket(owner,review['ticket_id'])
        document=self.source(owner,ticket)
        record=await self.current(owner,ticket['id'])
        if (review['document_id']!=document['id'] or review['sha256']!=document['sha256']
                or review['signature']!=self.shape_key(record['snapshot']) or review['scope']!=self.billing.portal_permissions.scope(ticket)):
            raise AppError(409,'portal_changed','Cambió el ticket o formulario. Revisa los campos actuales.')
        self.store.claim('portal_data_review',owner,identifier)
        return await self.read_fields(owner,ticket['id'],record['snapshot'],consent=True)

    async def read_fields(self,owner,ticket_id,snapshot,*,consent=False):
        if not public_form_ready(snapshot):
            raise AppError(409,'portal_form','Primero encuentra un formulario público utilizable.')
        return await self._read(owner,ticket_id,self.receipt_fields(snapshot),self.shape_key(snapshot),consent=consent)

    async def read_for_email(self,owner,ticket_id):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        if ticket.get('billing_url') or not ticket.get('email_explicitly_for_billing') or not ticket.get('billing_email'):
            return False
        fields=[{'key':key,'label':label} for key,label in [('folio','Folio'),('purchase_date','Fecha'),('total','Importe')]]
        return await self._read(owner,ticket_id,fields,self.store.digest('email-receipt-fields-v1'))

    async def _read(self,owner,ticket_id,fields,signature,*,consent=False):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        if not hasattr(self.billing.extractor,'extract_fields'):return False
        if not fields:return False
        if not consent and ticket.get('portal_data_signature')==signature and ticket.get('portal_data_document_id')==ticket['document_id']:
            return False
        if not consent and not ticket.get('portal_driven') and all(ticket.get(item['key']) for item in fields):
            return False
        document=self.source(owner,ticket)
        if not consent and document.get('analysis_scope')!='portal-fields-v1':return False
        lookup=self.store.digest('portal-data:'+ticket['id']+document['sha256']+signature)
        previous=self.store.find('portal_data',owner,lookup)
        if previous:return False
        if self.billing.analysis_lock.locked():
            raise AppError(429,'billing_busy','Hay otro documento en análisis. Espera unos segundos.')
        async with self.billing.analysis_lock:
            attempt='portal:'+lookup
            day=datetime.now(timezone.utc).date().isoformat()
            with self.billing.database.connection(True) as conn:
                used=conn.execute('SELECT COUNT(*) FROM billing_extractions WHERE owner=? AND day=?',(owner,day)).fetchone()[0]
                if used>=self.billing.settings.billing_max_daily_extractions:
                    raise AppError(429,'billing_daily_limit','Alcanzaste el límite diario de lecturas de documentos.')
                try:conn.execute('INSERT INTO billing_extractions VALUES(?,?,?,?)',(owner,day,attempt,'started'))
                except sqlite3.IntegrityError:
                    raise AppError(409,'extraction_attempted','Esta lectura ya tuvo un intento; completa solo los campos pendientes.') from None
            record=self.store.add('portal_data',owner,{'ticket_id':ticket['id'],'signature':signature,'status':'started'},lookup=lookup)
            try:
                result=await self.billing.extractor.extract_fields(document['mime'],base64.b64decode(document['data']),fields)
                data=TicketExtraction.model_validate(result).model_dump()
                if not data['is_ticket']:
                    raise AppError(422,'portal_not_ticket','El archivo no se identificó como ticket. Revisa el adjunto.')
                current=self.billing.resolve_ticket(owner,ticket['id'])
                if current['id']!=ticket['id'] or current['document_id']!=document['id'] or current['profile_id']!=ticket['profile_id']:
                    raise AppError(409,'portal_changed','Cambió el archivo o perfil durante la lectura. Revisa el ticket.')
                ticket=current
                if document.get('portal_replacement'):
                    for key in ('folio','purchase_date','total','issuer_rfc'):
                        old,new=ticket.get(key),data.get(key)
                        if old and new:
                            same=Decimal(old)==Decimal(new) if key=='total' else str(old).strip().casefold()==str(new).strip().casefold()
                            if not same:
                                raise AppError(409,'portal_source_mismatch','La foto no coincide con los datos del ticket guardado. Revisa el archivo antes de continuar.')
                # Un dato ya completado por la persona nunca se reemplaza por IA.
                protected=set(ticket.get('fields_completed_by_user',[]))
                allowed={item['key'] for item in fields}|IDENTITY_KEYS
                for key in allowed-protected:
                    if data.get(key) is not None and not (key=='merchant' and ticket.get('merchant')):
                        ticket[key]=data[key]
                ticket.update(portal_stage='data_read',portal_data_signature=signature,portal_data_document_id=document['id'],portal_data_error=None)
                ticket=self.billing._status(owner,ticket,Status.DATOS_EXTRAIDOS)
                # Conservar la identidad semántica para no abrir dos trámites del mismo ticket.
                if not missing_receipt_fields(ticket):
                    merchant=(ticket.get('issuer_rfc') or ticket.get('merchant') or '').strip().upper()
                    key,_,reference=purchase_reference(ticket)
                    identity_ref=(reference or '').strip().upper() if key=='folio' else (key or '')+':'+(reference or '').strip()
                    amount=format(Decimal(ticket['total']).normalize(),'f')
                    identity=self.store.digest('|'.join([merchant,ticket['purchase_date'],identity_ref,amount]))
                    duplicate=self.store.find('ticket',owner,identity)
                    if duplicate and duplicate['id']!=ticket['id']:
                        if duplicate['profile_id']!=ticket['profile_id']:
                            raise AppError(409,'ticket_profile','Este ticket ya está asociado a otro perfil. Revisa su solicitud.')
                        ticket['superseded_by']=duplicate['id']
                        self.store.update('ticket',owner,ticket['id'],ticket)
                    else:self.store.change_lookup('ticket',owner,ticket['id'],identity)
                self.store.update('portal_data',owner,record['id'],record|{'status':'complete'})
                with self.billing.database.connection(True) as conn:
                    conn.execute("UPDATE billing_extractions SET status='complete' WHERE owner=? AND document_id=?",(owner,attempt))
                return True
            except Exception as error:
                failure=error if isinstance(error,AppError) else AppError(422,'extraction_format','No se completó la lectura de campos. El archivo se conserva.')
                self.store.update('portal_data',owner,record['id'],record|{'status':'failed','code':failure.code})
                ticket=self.billing.resolve_ticket(owner,ticket['id'])
                ticket['portal_data_error']={'code':failure.code,'message':failure.message}
                self.store.update('ticket',owner,ticket['id'],ticket)
                with self.billing.database.connection(True) as conn:
                    conn.execute("UPDATE billing_extractions SET status='failed' WHERE owner=? AND document_id=?",(owner,attempt))
                raise failure from None

    def plan(self,owner,ticket,snapshot):
        profile=self.store.get('profile',owner,ticket['profile_id'])
        missing=[];values=[]
        if not public_form_ready(snapshot):return [],['Formulario no disponible']
        if snapshot.get('partial'):return [],['Vista parcial del formulario']
        missing.extend(snapshot.get('unsupported_required',[]))
        for item in snapshot['fields']:
            key=item.get('key')
            raw=profile.get(key) if key in PROFILE_KEYS else ticket.get(key) if key in RECEIPT_KEYS else None
            value=portal_value(item,raw)
            if value and item.get('max_length') is not None and len(value)>item['max_length']:
                missing.append(item['label'])
            elif value and item.get('reference'):
                values.append({'reference':item['reference'],'value':value,'label':item['label'],'key':key})
            elif item['required']:
                missing.append(item['label'])
        return values,list(dict.fromkeys(missing))

    def fill_request(self,owner,ticket_id,snapshot):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        self.billing.portal_permissions.require(owner,ticket)
        values,missing=self.plan(owner,ticket,snapshot)
        if missing or not values:return None
        signature=self.shape_key(snapshot)
        fingerprint=self.store.digest(json.dumps({'form':signature,'profile':ticket['profile_id'],
            'document':ticket['document_id'],'values':[{k:v for k,v in item.items() if k!='reference'} for item in values]},sort_keys=True))
        lookup=self.store.digest('portal-fill:'+ticket['id']+fingerprint)
        old=self.store.find('portal_fill_review',owner,lookup)
        if old:
            if old['_state']=='pending':return old
            if old['session_id']==snapshot['session_id']:return None
            self.store.change_lookup('portal_fill_review',owner,old['id'],self.store.digest('old-fill:'+old['id']))
        return self.store.add('portal_fill_review',owner,{'ticket_id':ticket['id'],'signature':signature,
            'fingerprint':fingerprint,'scope':self.billing.portal_permissions.scope(ticket),'values':values,
            'session_id':snapshot['session_id'],'observation_id':snapshot['observation_id'],'page':snapshot.get('bookmark_url') or snapshot['page']},
            lookup=lookup,state='pending',expires=time.time()+self.billing.settings.approval_ttl_seconds)

    async def fill_approved(self,owner,identifier):
        review=self.store.get('portal_fill_review',owner,identifier)
        if review['_state']!='pending':
            raise AppError(409,'billing_approval','Este llenado ya se intentó o canceló. Consulta su estado.')
        ticket=self.billing.resolve_ticket(owner,review['ticket_id'])
        record=await self.current(owner,ticket['id'],refresh=True)
        ticket=self.billing.resolve_ticket(owner,ticket['id'])
        fresh=self.fill_request(owner,ticket['id'],record['snapshot'])
        if not fresh or fresh['fingerprint']!=review['fingerprint'] or review['scope']!=self.billing.portal_permissions.scope(ticket):
            raise AppError(409,'portal_changed','Cambió un dato o el formulario. Revisa antes de llenar.')
        self.store.claim('portal_fill_review',owner,identifier)
        values,_=self.plan(owner,ticket,record['snapshot'])
        review.update(session_id=record['snapshot']['session_id'],observation_id=record['snapshot']['observation_id'],values=values)
        self.store.update('portal_fill_review',owner,identifier,review)
        try:
            result=await self.billing.browser.fill(owner,review['session_id'],review['observation_id'],
                [{'reference':item['reference'],'value':item['value']} for item in values])
        except Exception as error:
            self.store.update('portal_fill_review',owner,identifier,review,state='unknown')
            ticket['portal_fill']={'review_id':identifier,'session_id':review['session_id'],'state':'unknown','submitted':False}
            self.store.update('ticket',owner,ticket['id'],ticket)
            raise error
        if result['observation_id']!=review['observation_id'] or set(result['filled_references'])!={item['reference'] for item in values}:
            self.store.update('portal_fill_review',owner,identifier,review,state='unknown')
            raise AppError(502,'browser_boundary','No se confirmó el llenado completo. Consulta su estado.')
        review.update(result=result,filled_at=time.time())
        self.store.update('portal_fill_review',owner,identifier,review,state='complete')
        ticket=self.billing.resolve_ticket(owner,ticket['id'])
        ticket['portal_fill']={'review_id':identifier,'session_id':review['session_id'],'filled_at':time.time(),'submitted':False,'network_frozen':True}
        self.store.update('ticket',owner,ticket['id'],ticket)
        self.store.audit(owner,ticket['id'],'portal_filled_offline')
        return review

    def replace_source(self,owner,ticket_id,data):
        from .documents import inspect_document
        import hashlib
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        if ticket.get('delivery'):
            raise AppError(409,'portal_source','El ticket ya tiene una solicitud enviada. Revisa su estado.')
        mime,content=inspect_document(data,self.billing.settings.billing_max_upload_bytes)
        sha=hashlib.sha256(content).hexdigest()
        lookup=self.store.digest('portal-source:'+ticket['id']+sha)
        document=self.store.find('document',owner,lookup)
        if not document:
            if self.store.count('document',owner)>=self.billing.settings.billing_max_documents:
                raise AppError(429,'document_limit','Se alcanzó el límite de archivos activos.')
            document=self.store.add('document',owner,{'purpose':'ticket','mime':mime,'data':base64.b64encode(content).decode(),
                'profile_id':ticket['profile_id'],'ticket_id':ticket['id'],'sha256':sha,'analysis_consent':False,
                'extracted':None,'portal_replacement':True},lookup=lookup,
                expires=time.time()+self.billing.settings.billing_ticket_retention_hours*3600)
        ticket.update(document_id=document['id'],portal_data_error=None)
        self.store.update('ticket',owner,ticket['id'],ticket)
        return document

    async def fill_status(self,owner,identifier):
        review=self.store.get('portal_fill_review',owner,identifier)
        result=await self.billing.browser.filled(owner,review['session_id'])
        if (result['observation_id']!=review['observation_id']
                or set(result['filled_references'])!={item['reference'] for item in review['values']}):
            raise AppError(502,'browser_boundary','El estado no corresponde al llenado autorizado.')
        if review['_state'] in {'unknown','claimed'}:
            self.store.update('portal_fill_review',owner,identifier,review|{'result':result},state='complete')
        return result
