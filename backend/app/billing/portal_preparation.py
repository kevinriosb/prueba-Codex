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
from .portal_inspection import field_key
from .portal_permissions import PortalPermissionRequired,portal_origin


RECEIPT_KEYS={'folio','account_number','reference_number','billing_code','web_id','store_number','register_number',
    'toll_station','lane','purchase_date','purchase_time','total','issuer_rfc'}
PROFILE_KEYS={'rfc','name','postal_code','fiscal_regime','cfdi_use','email'}
IDENTITY_KEYS={'merchant','issuer_rfc','purchase_date','folio','total','currency'}


def alternative_key(label):
    match=re.search(r'\b(?:no (?:cuento|cuenta|tengo|tiene|dispongo|dispone)|sin|without|don.?t have)\b(.*)',label,re.I)
    if not match:return None
    missing=re.split(r'[,;:]|\b(?:ingres\w*|enter|captur\w*|utili[cz]\w*|complete|registra\w*)\b',match.group(1),maxsplit=1,flags=re.I)[0]
    return field_key(missing)


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
            formats=[(r'dd\s*([/.-])\s*mm\s*\1\s*(?:aaaa|yyyy)',('%d','%m','%Y')),
                (r'mm\s*([/.-])\s*dd\s*\1\s*(?:aaaa|yyyy)',('%m','%d','%Y')),
                (r'(?:aaaa|yyyy)\s*([/.-])\s*mm\s*\1\s*dd',('%Y','%m','%d'))]
            choices={parsed.strftime(match.group(1).join(parts)) for pattern,parts in formats for match in re.finditer(pattern,hint)}
            if len(choices)>1:return None
            if choices:value=choices.pop()
        except ValueError:
            return None
    return value or None


class PortalPreparation:
    def __init__(self,billing):
        self.billing=billing
        self.store=billing.store

    def shape_key(self,snapshot):
        return self.store.digest(json.dumps(form_shape(snapshot),ensure_ascii=False,sort_keys=True))

    def describe_fields(self,owner,ticket_id,observation_id,meanings):
        """Asocia significado a etiquetas observadas; no acepta ni escribe valores."""
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        self.billing.portal_permissions.require(owner,ticket)
        record=self.billing.browser_review(owner,ticket['id'])
        snapshot=record['snapshot']
        if observation_id!=snapshot['observation_id'] or not 1<=len(meanings)<=24:
            raise AppError(409,'browser_stale','La página cambió. Revisa sus campos actuales.')
        observed={item.get('reference'):item for item in snapshot['fields'] if item.get('reference')}
        if len({item.reference for item in meanings})!=len(meanings):
            raise AppError(422,'portal_fields','Hay referencias de campo repetidas.')
        for meaning in meanings:
            field=observed.get(meaning.reference)
            if not field or field.get('key') not in {None,meaning.key}:
                raise AppError(409,'portal_fields','El significado no corresponde al campo observado.')
        for meaning in meanings:observed[meaning.reference]['key']=meaning.key
        if public_form_ready(snapshot):
            ticket['portal_stage']='fields_pending'
            ticket['portal_browser']['fields']=[{'key':item.get('key'),'label':item['label'],
                'required':item['required'],'matched':bool(ticket.get(item.get('key'))),'options':item['options']}
                for item in snapshot['fields']]
            self.billing._save_portal_checkpoint(owner,ticket,snapshot,record.get('brand_label'))
            self.store.update('ticket',owner,ticket['id'],ticket)
        return self.store.update('browser_review',owner,record['id'],record,version=record['_version'])

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

    def active_fields(self,ticket,snapshot):
        selected=ticket.get('portal_alternative') or {}
        valid=(selected.get('document_id')==ticket['document_id'] and selected.get('scope')==self.billing.portal_permissions.scope(ticket)
            and selected.get('signature')==self.shape_key(snapshot))
        return [item for item in snapshot['fields'] if not valid or item.get('key')!=selected.get('replaced_key')]

    def receipt_fields(self,snapshot,ticket=None):
        fields=self.active_fields(ticket,snapshot) if ticket else snapshot['fields']
        return [item for item in fields if item.get('key') in RECEIPT_KEYS]

    def replacement_pending(self,owner,ticket):
        if ticket.get('portal_source_pending'):return True
        if ticket.get('portal_data_document_id')==ticket['document_id']:return False
        try:document=self.billing.document(owner,ticket['document_id'])
        except AppError as error:
            if error.status==404:return False
            raise
        return document.get('portal_replacement') is True

    async def advance_alternative(self,owner,ticket_id,snapshot):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        if (not public_form_ready(snapshot) or ticket.get('portal_data_error')
                or ticket.get('portal_data_document_id')!=ticket['document_id']):return None
        choices=self.alternatives(ticket,snapshot)
        if len(choices)!=1:return None
        action=choices[0]
        target=action.get('permission_origin')
        if target and portal_origin(target) not in self.billing.portal_permissions.allowed(owner,ticket):
            raise PortalPermissionRequired([target])
        attempt=self.store.digest(ticket['document_id']+snapshot['session_id']+self.shape_key(snapshot)+action['label'])
        attempts=ticket.get('portal_alternative_attempts',[])
        if attempt in attempts or len(attempts)>=8:return None
        ticket['portal_alternative_attempts']=[*attempts,attempt]
        self.store.update('ticket',owner,ticket['id'],ticket,version=ticket['_version'])
        return await self.billing.browse_portal(owner,ticket['id'],observation_id=snapshot['observation_id'],reference=action['reference'])

    def alternatives(self,ticket,snapshot):
        if not public_form_ready(snapshot):return []
        missing={item['key'] for item in self.receipt_fields(snapshot,ticket) if not ticket.get(item['key'])}
        return [item for item in snapshot['actions'] if item['kind'] in {'expand','link'} and alternative_key(item['label']) in missing]

    def bind_alternative(self,ticket,previous,current,action):
        if action not in self.alternatives(ticket,previous):return
        key=alternative_key(action['label'])
        if public_form_ready(current) and any(item.get('key') in RECEIPT_KEYS-{key} for item in current['fields']):
            ticket['portal_alternative']={'document_id':ticket['document_id'],'scope':self.billing.portal_permissions.scope(ticket),
                'signature':self.shape_key(current),'replaced_key':key,'label':action['label']}

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
        fields=self.receipt_fields(snapshot,ticket)
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

    def receipt_update_request(self,owner,ticket_id):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        document=self.source(owner,ticket)
        fields=[{'key':key,'label':label} for key,label in [('folio','Folio'),('purchase_date','Fecha'),('total','Importe')]]
        for item in (ticket.get('portal_browser') or {}).get('fields',[]):
            if item.get('key') in RECEIPT_KEYS and not any(field['key']==item['key'] for field in fields):
                fields.append({'key':item['key'],'label':item['label']})
        signature=self.store.digest('receipt-update-v1:'+json.dumps(fields,sort_keys=True))
        scope=self.billing.portal_permissions.scope(ticket)
        lookup=self.store.digest('receipt-update:'+ticket['id']+document['id']+signature+scope)
        old=self.store.find('portal_data_review',owner,lookup)
        if old:return old
        return self.store.add('portal_data_review',owner,{'ticket_id':ticket['id'],'document_id':document['id'],
            'sha256':document['sha256'],'signature':signature,'scope':scope,
            'fields':fields,'receipt_update':True},lookup=lookup,state='pending',
            expires=time.time()+self.billing.settings.approval_ttl_seconds)

    async def read_approved(self,owner,identifier):
        review=self.store.get('portal_data_review',owner,identifier)
        ticket=self.billing.resolve_ticket(owner,review['ticket_id'])
        document=self.source(owner,ticket)
        if review.get('receipt_update'):
            if (review['document_id']!=document['id'] or review['sha256']!=document['sha256']
                    or review['scope']!=self.billing.portal_permissions.scope(ticket)):
                raise AppError(409,'portal_changed','Cambió la foto o solicitud. Usa la revisión actual.')
            self.store.claim('portal_data_review',owner,identifier)
            return await self._read(owner,ticket['id'],review['fields'],review['signature'],consent=True)
        record=await self.current(owner,ticket['id'])
        if (review['document_id']!=document['id'] or review['sha256']!=document['sha256']
                or review['signature']!=self.shape_key(record['snapshot']) or review['scope']!=self.billing.portal_permissions.scope(ticket)):
            raise AppError(409,'portal_changed','Cambió el ticket o formulario. Revisa los campos actuales.')
        self.store.claim('portal_data_review',owner,identifier)
        return await self.read_fields(owner,ticket['id'],record['snapshot'],consent=True)

    async def read_fields(self,owner,ticket_id,snapshot,*,consent=False):
        if not public_form_ready(snapshot):
            raise AppError(409,'portal_form','Primero encuentra un formulario público utilizable.')
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        return await self._read(owner,ticket_id,self.receipt_fields(snapshot,ticket),self.shape_key(snapshot),consent=consent)

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
        if (not consent and ticket.get('portal_data_document_id')==ticket['document_id']
                and all(ticket.get(item['key']) for item in fields)):
            return False
        if not consent and all(ticket.get(item['key']) and item['key'] in ticket.get('fields_completed_by_user',[]) for item in fields):
            return False
        if not consent and not ticket.get('portal_driven') and all(ticket.get(item['key']) for item in fields):
            return False
        if not consent and ticket.get('receipt_summary_read') and not self.replacement_pending(owner,ticket) and all(ticket.get(item['key']) for item in fields):
            return False
        document=self.source(owner,ticket)
        scope=document.get('analysis_scope')
        if not consent and scope not in {'portal-fields-v1','portal-fields-v2','portal-fields-v3'}:return False
        lookup=self.store.digest('portal-data:'+ticket['id']+document['sha256']+signature)
        previous=self.store.find('portal_data',owner,lookup)
        if previous:return False
        if not consent and scope=='portal-fields-v1' and (ticket.get('portal_data_document_id')==document['id']
                or any(record.get('ticket_id')==ticket['id'] and record.get('document_id') in {None,document['id']} for record in self.store.list('portal_data',owner))):
            return False  # El consentimiento anterior prometía dos lecturas.
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
            record=self.store.add('portal_data',owner,{'ticket_id':ticket['id'],'document_id':document['id'],'signature':signature,'status':'started'},lookup=lookup)
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
                ticket.update(portal_stage='data_read',portal_data_signature=signature,portal_data_document_id=document['id'],portal_data_error=None,portal_source_pending=False)
                selected=ticket.get('portal_alternative') or {}
                if selected.get('scope')==self.billing.portal_permissions.scope(ticket) and selected.get('signature')==signature:
                    selected['document_id']=document['id']
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
        if self.replacement_pending(owner,ticket):return [],['Foto reenviada pendiente de verificar']
        missing.extend(snapshot.get('unsupported_required',[]))
        for item in self.active_fields(ticket,snapshot):
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
            if isinstance(error,AppError) and (error.code in {'browser_stale','browser_intervention','browser_private'} or error.code=='browser_fill' and error.status==422):
                self.store.update('portal_fill_review',owner,identifier,review,state='rejected')
                raise
            self.store.update('portal_fill_review',owner,identifier,review,state='unknown')
            ticket['portal_fill']={'review_id':identifier,'session_id':review['session_id'],'state':'unknown','submitted':False}
            self.store.update('ticket',owner,ticket['id'],ticket)
            raise error
        if result['observation_id']!=review['observation_id'] or set(result['filled_references'])!={item['reference'] for item in values}:
            self.store.update('portal_fill_review',owner,identifier,review,state='unknown')
            ticket['portal_fill']={'review_id':identifier,'session_id':review['session_id'],'state':'unknown','submitted':False}
            self.store.update('ticket',owner,ticket['id'],ticket)
            raise AppError(502,'browser_boundary','No se confirmó el llenado completo. Consulta su estado.')
        review.update(result=result,filled_at=time.time())
        self.store.update('portal_fill_review',owner,identifier,review,state='complete')
        ticket=self.billing.resolve_ticket(owner,ticket['id'])
        ticket['portal_fill']={'review_id':identifier,'session_id':review['session_id'],'state':'complete','filled_at':time.time(),'submitted':False,'network_frozen':True}
        self.store.update('ticket',owner,ticket['id'],ticket)
        self.store.audit(owner,ticket['id'],'portal_filled_offline')
        return review

    def replace_source(self,owner,ticket_id,data,*,staged_id=None):
        from .documents import inspect_document
        import hashlib
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        tx=self.billing.portal_submission.current(owner,ticket['id'])
        if ticket.get('delivery') or tx and tx['state'] in {'unknown','submitted'}:
            raise AppError(409,'portal_source','El ticket ya tiene una solicitud enviada. Revisa su estado.')
        staged=self.billing.document(owner,staged_id) if staged_id else None
        if staged:
            # receive_document ya verificó y normalizó estos bytes. Repetir la
            # compresión JPEG cambia la huella y degrada una foto con detalle.
            content=base64.b64decode(staged['data'],validate=True)
            same_ticket=(staged['purpose']=='ticket' and staged.get('ticket_id')==ticket['id']
                and staged.get('profile_id')==ticket['profile_id'])
            if (staged['purpose']!='unclassified' and not same_ticket or content!=data
                    or hashlib.sha256(content).hexdigest()!=staged['sha256']):
                raise AppError(409,'portal_source','El archivo recibido cambió. Revisa el ticket.')
            mime=staged['mime']
        else:
            mime,content=inspect_document(data,self.billing.settings.billing_max_upload_bytes)
        sha=hashlib.sha256(content).hexdigest()
        lookup=self.store.digest('portal-source:'+ticket['id']+sha)
        document=self.store.find('document',owner,lookup)
        if not document and staged and staged['purpose']=='ticket':
            document=self.store.update('document',owner,staged['id'],staged|{'portal_replacement':True})
        if not document and staged:
            document=self.store.update('document',owner,staged['id'],staged|{'purpose':'ticket',
                'profile_id':ticket['profile_id'],'ticket_id':ticket['id'],'portal_replacement':True,'intended_purpose':None})
            self.store.change_lookup('document',owner,document['id'],lookup)
        if not document:
            if self.store.count('document',owner)>=self.billing.settings.billing_max_documents:
                raise AppError(429,'document_limit','Se alcanzó el límite de archivos activos.')
            document=self.store.add('document',owner,{'purpose':'ticket','mime':mime,'data':base64.b64encode(content).decode(),
                'profile_id':ticket['profile_id'],'ticket_id':ticket['id'],'sha256':sha,'analysis_consent':False,
                'extracted':None,'portal_replacement':True},lookup=lookup,
                expires=time.time()+self.billing.settings.billing_ticket_retention_hours*3600)
        signature=ticket.get('portal_data_signature')
        cached=self.store.find('portal_data',owner,self.store.digest('portal-data:'+ticket['id']+sha+signature)) if signature else None
        verified=bool(cached and cached.get('status')=='complete')
        ticket.update(document_id=document['id'],portal_data_error=None,portal_source_pending=not verified)
        if verified:
            ticket['portal_data_document_id']=document['id']
            selected=ticket.get('portal_alternative') or {}
            if selected.get('scope')==self.billing.portal_permissions.scope(ticket):selected['document_id']=document['id']
        self.store.update('ticket',owner,ticket['id'],ticket)
        return document

    def expired_fill(self,owner,identifier,ticket_id=None):
        for ticket in self.store.list('ticket',owner):
            saved=ticket.get('portal_fill') or {}
            if saved.get('review_id')==identifier:
                ticket['portal_fill']=saved|{'state':'expired'}
                self.store.update('ticket',owner,ticket['id'],ticket)
                return {'expired':True,'ticket_id':ticket['id']}
        if ticket_id:
            self.store.get('ticket',owner,ticket_id)
            return {'expired':True,'ticket_id':ticket_id}
        raise AppError(404,'billing_not_found','Registro no encontrado, eliminado o caducado.')

    async def fill_status(self,owner,identifier):
        try:review=self.store.get('portal_fill_review',owner,identifier)
        except AppError as error:
            if error.status!=404:raise
            return self.expired_fill(owner,identifier)
        try:result=await self.billing.browser.filled(owner,review['session_id'])
        except AppError as error:
            if error.code!='browser_session':raise
            return self.expired_fill(owner,identifier,review['ticket_id'])
        if (result['observation_id']!=review['observation_id']
                or set(result['filled_references'])!={item['reference'] for item in review['values']}):
            raise AppError(502,'browser_boundary','El estado no corresponde al llenado autorizado.')
        if review['_state'] in {'unknown','claimed'}:
            self.store.update('portal_fill_review',owner,identifier,review|{'result':result},state='complete')
            ticket=self.billing.resolve_ticket(owner,review['ticket_id'])
            ticket['portal_fill']={'review_id':identifier,'session_id':review['session_id'],'state':'complete',
                'filled_at':time.time(),'submitted':False,'network_frozen':True}
            self.store.update('ticket',owner,ticket['id'],ticket)
        return result
