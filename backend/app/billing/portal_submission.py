"""Orquestación de envío real del formulario y recepción del CFDI, por pasos confirmados."""
import base64
import hashlib
import json
import time
from datetime import datetime,timezone

from ...browser_worker.policy import origin
from ..errors import AppError
from .cfdi import validate_cfdi
from .documents import inspect_document
from .models import Status
from .portal_preparation import PROFILE_KEYS,RECEIPT_KEYS,portal_value
from .receipt import purchase_date,purchase_total


class PortalSubmission:
    def __init__(self,billing):
        self.billing,self.store=billing,billing.store

    def guard(self,owner,ticket):
        if ticket.get('telegram_workflow') in {'paused','cancelled'}:
            raise AppError(409,'portal_inactive','Retoma esta solicitud antes de continuar.')
        state=self.store.find('channel_state',owner,'telegram')
        if state and (state.get('active_ticket_id')!=ticket['id'] or state.get('workflow_choice_id')):
            raise AppError(409,'portal_inactive','Elige la solicitud activa antes de enviar.')
        if ticket.get('delivery'):
            raise AppError(409,'already_requested','La solicitud ya se envió por correo. Consulta su estado.')

    def current(self,owner,ticket_id):
        return self.store.find('portal_transaction',owner,self.store.digest('portal-transaction:'+ticket_id))

    def plan(self,owner,ticket,view,manual=None):
        snapshot=view['snapshot'];profile=self.store.get('profile',owner,ticket['profile_id'])
        manual=manual or {};values=[];missing=[]
        if snapshot['network_blocked'] or snapshot['captcha'] or snapshot.get('login_required') or snapshot.get('partial') or snapshot.get('unsupported_required'):
            return [],[{'reference':None,'label':'El portal requiere revisión manual','key':None}]
        for item in snapshot['fields']:
            key=item.get('key')
            value=portal_value(item,profile.get(key) if key in PROFILE_KEYS else ticket.get(key) if key in RECEIPT_KEYS else None)
            value=manual.get(self.field_identity(item)) or value
            if value and item.get('max_length') is not None and len(value)>item['max_length']:value=None
            if value and item.get('reference'):
                values.append({'reference':item['reference'],'label':item['label'],'key':key,'value':value})
            elif item['required']:
                missing.append(item)
        return values,missing

    def field_identity(self,item):
        return self.store.digest(json.dumps({key:item.get(key) for key in ('label','key','kind','input_type','options','max_length')},sort_keys=True))

    def fingerprint(self,owner,ticket,view,values):
        return self.store.digest(json.dumps({'scope':self.billing.portal_permissions.scope(ticket),
            'document':ticket['document_id'],'profile':self.store.get('profile',owner,ticket['profile_id']),
            'values':values,'form':self.billing.portal_preparation.shape_key(view['snapshot'])},sort_keys=True))

    async def review(self,owner,ticket_id):
        ticket=self.billing.resolve_ticket(owner,ticket_id)
        self.guard(owner,ticket)
        previous=self.current(owner,ticket['id'])
        saved=previous
        if previous and previous.get('state') in {'unknown','submitted'}:return previous
        if previous:
            try:view=await self.billing.browser.transaction_view(owner,previous['session_id'])
            except AppError as error:
                if error.code!='browser_session':raise
                if previous.get('private'):
                    previous['state']='expired'
                    return self.store.update('portal_transaction',owner,previous['id'],previous)
                previous=None
        if not previous:
            record=await self.billing.portal_preparation.current(owner,ticket['id'],refresh=True)
            view=await self.billing.browser.transaction_view(owner,record['snapshot']['session_id'])
        payload={'ticket_id':ticket['id'],'session_id':view['snapshot']['session_id'],'scope':self.billing.portal_permissions.scope(ticket),
            'state':'review','private':not view['snapshot']['public_only'],'view':view,'manual':saved.get('manual',{}) if saved else {}}
        if not previous:payload['operation_id']=None
        if saved:return self.store.update('portal_transaction',owner,saved['id'],saved|payload)
        return self.store.add('portal_transaction',owner,payload,lookup=self.store.digest('portal-transaction:'+ticket['id']))

    def approvals(self,owner,transaction):
        ticket=self.billing.resolve_ticket(owner,transaction['ticket_id'])
        values,missing=self.plan(owner,ticket,transaction['view'],transaction.get('manual'))
        actions=[a for a in transaction['view']['actions'] if a['kind']!='download']
        if missing:return [],missing,values
        records=[]
        for action in actions:
            snapshot=transaction['view']['snapshot']
            origins=sorted({origin(snapshot['page']),action['origin'],*[origin(f['source_url']) for f in snapshot['fields'] if f.get('source_url')]})
            fingerprint=self.fingerprint(owner,ticket,transaction['view'],values)
            lookup=self.store.digest('portal-send:'+snapshot['session_id']+snapshot['observation_id']+action['reference']+fingerprint)
            record=self.store.find('portal_send_review',owner,lookup)
            if not record:
                record=self.store.add('portal_send_review',owner,{'ticket_id':ticket['id'],'transaction_id':transaction['id'],
                    'session_id':snapshot['session_id'],'observation_id':snapshot['observation_id'],'action':action,'origins':origins,
                    'values':values,'fingerprint':fingerprint,'scope':self.billing.portal_permissions.scope(ticket),
                    'fiscal_data':self.store.get('profile',owner,ticket['profile_id']),
                    'deadline':time.time()+900},state='pending',lookup=lookup)
            if record['_state']=='pending':records.append(record)
        return records,missing,values

    async def send(self,owner,identifier):
        approval=self.store.get('portal_send_review',owner,identifier)
        ticket=self.billing.resolve_ticket(owner,approval['ticket_id'])
        self.guard(owner,ticket)
        tx=self.store.get('portal_transaction',owner,approval['transaction_id'])
        if approval['_state']!='pending':return await self.status(owner,tx)
        values,missing=self.plan(owner,ticket,tx['view'],tx.get('manual'))
        if (tx['state'] not in {'review','correction'} or approval['deadline']<time.time() or missing
                or approval['observation_id']!=tx['view']['snapshot']['observation_id']
                or approval['fingerprint']!=self.fingerprint(owner,ticket,tx['view'],values)
                or approval['scope']!=self.billing.portal_permissions.scope(ticket)):
            raise AppError(409,'portal_changed','Cambiaron los datos o el paso. Revisa antes de enviar.')
        self.store.claim('portal_send_review',owner,identifier)
        was_private=tx.get('private',False)
        tx.update(state='unknown',operation_id=identifier,private=True)
        self.store.update('portal_transaction',owner,tx['id'],tx)
        self.store.audit(owner,ticket['id'],'portal_step_authorized')
        try:
            result=await self.billing.browser.transaction_send(owner,approval['session_id'],operation_id=identifier,
                observation_id=approval['observation_id'],reference=approval['action']['reference'],origins=approval['origins'],
                values=[{k:v for k,v in item.items() if k in {'reference','value'}} for item in values])
        except AppError as error:
            if error.code in {'browser_stale','browser_intervention','browser_session'} or error.status==422:
                self.store.update('portal_send_review',owner,identifier,approval,state='rejected')
                tx['state']='expired' if error.code=='browser_session' else 'review'
                tx['private']=was_private
                self.store.update('portal_transaction',owner,tx['id'],tx)
                raise
            self.store.update('portal_send_review',owner,identifier,approval,state='unknown')
            return tx
        return await self.accept(owner,tx,approval,result)

    async def status(self,owner,tx):
        if tx.get('operation_id') and tx['state'] in {'unknown','submitted'}:
            try:result=await self.billing.browser.transaction_status(owner,tx['session_id'],tx['operation_id'])
            except AppError:return tx
            approval=self.store.get('portal_send_review',owner,tx['operation_id'])
            return await self.accept(owner,tx,approval,result)
        return tx

    async def accept(self,owner,tx,approval,result):
        if result['operation_id']!=approval['id'] or result['view']['snapshot']['session_id']!=tx['session_id']:
            raise AppError(502,'portal_result','La respuesta no corresponde al paso autorizado.')
        tx.update(state=result['state'],view=result['view'])
        tx=self.store.update('portal_transaction',owner,tx['id'],tx)
        self.store.update('portal_send_review',owner,approval['id'],approval,state='unknown' if result['state']=='unknown' else 'complete')
        ticket=self.billing.resolve_ticket(owner,tx['ticket_id'])
        if result['state'] in {'unknown','submitted'}:
            ticket['portal_delivery']={'state':result['state'],'operation_id':approval['id'],'session_id':tx['session_id'],
                'fiscal_data':approval['fiscal_data'],'at':time.time()}
            self.store.update('ticket',owner,ticket['id'],ticket)
        elif (ticket.get('portal_delivery') or {}).get('operation_id')==approval['id']:
            ticket.pop('portal_delivery',None)
            self.store.update('ticket',owner,ticket['id'],ticket)
        if result['state']=='submitted':
            try:await self.receive_files(owner,tx)
            except AppError as error:
                tx['file_error']=error.message
                self.store.update('portal_transaction',owner,tx['id'],tx)
        self.store.audit(owner,ticket['id'],'portal_step_'+result['state'])
        return tx

    async def receive_files(self,owner,tx):
        ticket=self.billing.resolve_ticket(owner,tx['ticket_id'])
        profile=(ticket.get('portal_delivery') or {}).get('fiscal_data') or self.store.get('profile',owner,ticket['profile_id'])
        prepared=[];validation=None;expected_uuid=(ticket.get('validation') or {}).get('uuid')
        for item in tx['view']['files']:
            lookup=self.store.digest('portal-invoice:'+ticket['id']+item['sha256'])
            existing=self.store.find('document',owner,lookup)
            record=self.billing.document(owner,existing['id']) if existing else await self.billing.browser.transaction_file(owner,tx['session_id'],item['reference'])
            try:data=base64.b64decode(record['data'],validate=True)
            except (ValueError,KeyError):raise AppError(502,'portal_file','El portal devolvió un archivo incompleto.') from None
            if record['mime']!=item['mime'] or hashlib.sha256(data).hexdigest()!=item['sha256']:
                raise AppError(502,'portal_file','El archivo no coincide con el documento del portal.')
            mime,data=inspect_document(data,self.billing.settings.billing_max_upload_bytes,allow_xml=True)
            if mime=='application/xml':
                validation=validate_cfdi(data,profile,ticket)
                if expected_uuid and expected_uuid!=validation['uuid']:
                    raise AppError(409,'conflicting_invoice','El portal devolvió un UUID distinto. Revisa los archivos.')
                expected_uuid=validation['uuid']
                if (ticket.get('validation') or {}).get('uuid')==expected_uuid and ticket['validation'].get('sat_verified'):
                    validation=ticket['validation']
                duplicate=self.store.find('invoice_uuid',owner,self.store.digest(validation['uuid']))
                if duplicate and duplicate['ticket_id']!=ticket['id']:
                    raise AppError(409,'duplicate_invoice','Ese UUID ya corresponde a otra solicitud.')
            elif mime!='application/pdf':continue
            if existing:
                if existing['id'] not in ticket['invoice_documents']:ticket['invoice_documents'].append(existing['id'])
            else:prepared.append((mime,data,lookup))
        if self.store.count('document',owner)+len(prepared)>self.billing.settings.billing_max_documents:
            raise AppError(429,'document_limit','La factura se solicitó; falta espacio para guardar los archivos.')
        for mime,data,lookup in prepared:
            record=self.store.add('document',owner,{'purpose':'invoice_xml' if mime=='application/xml' else 'invoice_pdf',
                'mime':mime,'data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest(),
                'ticket_id':ticket['id'],'profile_id':ticket['profile_id']},lookup=lookup,
                expires=time.time()+self.billing.settings.billing_invoice_retention_days*86400)
            ticket['invoice_documents'].append(record['id'])
        if validation:
            ticket['validation']=validation
            lookup=self.store.digest(validation['uuid'])
            if not self.store.find('invoice_uuid',owner,lookup):self.store.add('invoice_uuid',owner,{'ticket_id':ticket['id']},lookup=lookup)
        self.billing._status(owner,ticket,Status.FACTURA_RECIBIDA if ticket.get('validation') else Status.ESPERANDO_RESPUESTA,
            'XML recibido y cotejado; falta consultar vigencia SAT.' if ticket.get('validation') else 'Solicitud enviada al portal. Esperando XML y PDF.')
        if (ticket.get('validation') or {}).get('sat_verified'):
            self.billing._finish_verified_invoice(owner,ticket)

    def set_field(self,owner,transaction_id,reference,value):
        tx=self.store.get('portal_transaction',owner,transaction_id)
        ticket=self.billing.resolve_ticket(owner,tx['ticket_id']);self.guard(owner,ticket)
        if tx['state'] not in {'review','correction'}:raise AppError(409,'portal_field','Este envío no admite cambios; revisa su estado.')
        field=next((f for f in tx['view']['snapshot']['fields'] if f['reference']==reference),None)
        if not field:raise AppError(409,'portal_field','El campo cambió. Actualiza este paso.')
        key=field.get('key')
        if key in PROFILE_KEYS:
            raise AppError(409,'portal_profile','Actualiza y confirma ese dato en Mi perfil fiscal.')
        value=value.strip()
        if not value or len(value)>250 or any(ord(c)<32 for c in value):raise AppError(422,'portal_field','Escribe únicamente el valor del campo.')
        if field.get('max_length') is not None and len(value)>field['max_length']:raise AppError(422,'portal_field','El dato supera el tamaño del campo.')
        if field['input_type']=='date':
            try:value=datetime.strptime(value,'%d/%m/%Y').date().isoformat() if '/' in value else datetime.strptime(value,'%Y-%m-%d').date().isoformat()
            except ValueError:raise AppError(422,'portal_field','Usa una fecha válida: DD/MM/AAAA.') from None
        if key in RECEIPT_KEYS:
            canonical=value
            if key=='purchase_date':
                try:canonical=purchase_date(value) or datetime.strptime(value,'%d/%m/%Y').date().isoformat()
                except ValueError:canonical=None
            elif key=='total':canonical=purchase_total(value)
            maximum=100 if key in {'folio','account_number','store_number'} else 13 if key=='issuer_rfc' else 160
            if not canonical or len(canonical)>maximum:raise AppError(422,'portal_field','El dato no tiene un formato válido.')
            ticket[key]=canonical
            ticket['fields_completed_by_user']=list(dict.fromkeys([*ticket.get('fields_completed_by_user',[]),key]))
            self.store.update('ticket',owner,ticket['id'],ticket)
            value=portal_value(field,canonical)
        tx.setdefault('manual',{})[self.field_identity(field)]=value
        return self.store.update('portal_transaction',owner,tx['id'],tx)

    def receive_crop(self,owner,tx_id,reference,data):
        tx=self.store.get('portal_transaction',owner,tx_id)
        ticket=self.billing.resolve_ticket(owner,tx['ticket_id']);self.guard(owner,ticket)
        if tx['state'] not in {'review','correction'}:raise AppError(409,'portal_field','Este paso ya no admite una foto.')
        field=next((f for f in tx['view']['snapshot']['fields'] if f['reference']==reference),None)
        if not field:raise AppError(409,'portal_field','El campo cambió.')
        if self.store.count('portal_field_photo',owner)>=10:
            raise AppError(429,'document_limit','Hay demasiados recortes pendientes. Escribe este dato manualmente.')
        mime,content=inspect_document(data,self.billing.settings.billing_max_upload_bytes)
        return self.store.add('portal_field_photo',owner,{'ticket_id':ticket['id'],'transaction_id':tx_id,'reference':reference,
            'label':field['label'],'key':field.get('key'),'mime':mime,'data':base64.b64encode(content).decode()},
            state='pending',expires=time.time()+900)

    async def read_crop(self,owner,identifier):
        photo=self.store.get('portal_field_photo',owner,identifier)
        ticket=self.billing.resolve_ticket(owner,photo['ticket_id']);self.guard(owner,ticket)
        tx=self.store.get('portal_transaction',owner,photo['transaction_id'])
        if tx['state'] not in {'review','correction'} or not any(f['reference']==photo['reference'] for f in tx['view']['snapshot']['fields']):
            raise AppError(409,'portal_field','Cambió el paso. La foto no se analizará.')
        if not hasattr(self.billing.extractor,'extract_field'):raise AppError(503,'portal_field','La lectura de recortes no está disponible; escribe el dato.')
        if self.billing.analysis_lock.locked():raise AppError(429,'billing_busy','Hay otra lectura en curso. Espera unos segundos.')
        async with self.billing.analysis_lock:
            day=datetime.now(timezone.utc).date().isoformat()
            with self.billing.database.connection(True) as conn:
                count=conn.execute('SELECT COUNT(*) FROM billing_extractions WHERE owner=? AND day=?',(owner,day)).fetchone()[0]
                if count>=self.billing.settings.billing_max_daily_extractions:raise AppError(429,'billing_daily_limit','Alcanzaste el límite de lecturas. Escribe el dato manualmente.')
            self.store.claim('portal_field_photo',owner,identifier)
            with self.billing.database.connection(True) as conn:
                conn.execute('INSERT INTO billing_extractions VALUES(?,?,?,?)',(owner,day,'field:'+identifier,'started'))
            result=await self.billing.extractor.extract_field(photo['mime'],base64.b64decode(photo['data']),photo['label'],photo.get('key'))
            value=result.value
            photo.pop('data',None)
            photo.update(value=value)
            self.store.update('portal_field_photo',owner,identifier,photo,state='read')
            return photo
