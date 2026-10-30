import asyncio
import base64
import hashlib
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from email.message import EmailMessage

from ...browser_worker.policy import bookmark_url,RESUMABLE_LIMITS
from ..errors import AppError
from .cfdi import SatVerifier, validate_cfdi
from .channels import official_contacts
from .documents import inspect_document
from .extraction import DocumentExtractor, EXTRACTION_VERSION
from .models import CFDI_USES, REGIMES, FiscalExtraction, Status, TicketExtraction, missing_fiscal_fields, regime_code, validate_fiscal
from .providers import GmailProvider, normalized_email
from .receipt import browser_missing_fields, missing_receipt_fields, pending_receipt_fields, purchase_date, purchase_reference, purchase_total, receipt_qr_urls,unrelated_billing_link
from .portal_inspection import PortalInspector, portal_plan
from .browser import PortalBrowserClient
from .portal_permissions import PortalPermissions,PortalPermissionRequired,portal_origin
from .portal_memory import PortalMemory,public_form_ready,stable_form_bookmark
from .store import BillingStore
from .portal_preparation import PortalPreparation
from .portal_submission import PortalSubmission
from .portal_discovery import PortalDiscovery


def public(record):
    return {key:value for key,value in record.items() if not key.startswith('_')}


class BillingService:
    def __init__(self,settings,database,agent_service,*,extractor=None,mail=None,portals=(),sat=None,browser=None):
        self.settings,self.database=settings,database
        self.enabled=bool(settings.billing_encryption_key.get_secret_value())
        self.store=None
        self.analysis_lock=asyncio.Lock()
        self.poll_lock=asyncio.Lock()
        self.notifier=None
        if self.enabled:
            self.store=BillingStore(database,settings.billing_encryption_key.get_secret_value())
            self.extractor=extractor or DocumentExtractor(agent_service,settings)
            self.mail=mail or GmailProvider(settings,self.store)
            self.sat=sat or SatVerifier(settings.billing_sat_enabled)
            self.portals=tuple(portals)
            self.portal_inspector=PortalInspector(settings)
            self.browser=browser or PortalBrowserClient(settings)
            self.portal_permissions=PortalPermissions(self)
            self.portal_memory=PortalMemory(self)
            self.portal_preparation=PortalPreparation(self)
            self.portal_submission=PortalSubmission(self)
            self.portal_discovery=PortalDiscovery(self)
            self.contacts=official_contacts(settings.billing_verified_contacts_json)
            self.store.purge()

    async def close(self):
        if self.enabled:
            for component in (self.mail,self.sat,self.portal_inspector,self.browser):
                close=getattr(component,'close',None)
                if close:
                    await close()

    def require_enabled(self):
        if not self.enabled:
            raise AppError(503,'billing_not_configured','Configura BILLING_ENCRYPTION_KEY para activar el almacenamiento fiscal cifrado.')
        self.store.purge()

    def overview(self,owner):
        with self.database.connection() as conn:
            exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='billing_records'").fetchone()
            encrypted=bool(exists and conn.execute('SELECT 1 FROM billing_records LIMIT 1').fetchone())
        result={'enabled':self.enabled,'cfdi_uses':CFDI_USES,'fiscal_regimes':REGIMES,
            'has_encrypted_records':encrypted,
            'background_tracking_enabled':self.settings.billing_background_tracking_enabled,
            'tracking_interval_seconds':self.settings.billing_tracking_interval_seconds,
            'tracking_max_checks':self.settings.billing_tracking_max_checks,
            'max_upload_bytes':self.settings.billing_max_upload_bytes,'email_send_enabled':self.settings.billing_live_email_enabled,
            'sat_enabled':self.settings.billing_sat_enabled,'reminders_enabled':False,
            'gmail_configured':False,'gmail_connected':False,'gmail_email':None,'profiles':[],'tickets':[],'documents':[]}
        if self.enabled:
            self.require_enabled()
            account=self.mail.account(owner)
            result.update(gmail_configured=self.mail.configured,gmail_connected=self.mail.ready(owner),
                gmail_email=account.get('email') if account else None,
                profiles=[public(value) for value in self.store.list('profile',owner)],
                tickets=[public(value) for value in self.store.list('ticket',owner)],
                documents=[{'id':value['id'],'purpose':value['purpose'],'expires':value['_expires']} for value in self.store.list('document',owner)])
        return result

    def document(self,owner,identifier):
        self.require_enabled()
        return self.store.get('document',owner,identifier)

    def resolve_ticket(self,owner,identifier):
        """Sigue solo sustituciones del mismo propietario, sin analizar ni reenviar."""
        seen=set()
        while identifier and identifier not in seen and len(seen)<10:
            seen.add(identifier)
            ticket=self.store.get('ticket',owner,identifier)
            if not ticket.get('superseded_by'):
                return ticket
            identifier=ticket['superseded_by']
        raise AppError(409,'ticket_reference','No se pudo resolver el ticket. Consulta /tickets.')

    def receive_document(self,owner,purpose,data,*,profile_id=None):
        self.require_enabled()
        if purpose not in {'csf','ticket','unclassified'}:
            raise AppError(422,'document_purpose','Indica si el archivo es una constancia o un ticket.')
        if profile_id:
            self.store.get('profile',owner,profile_id)
        if purpose=='ticket':
            if not profile_id:
                raise AppError(409,'fiscal_profile_required','Primero envía tu Constancia de Situación Fiscal y confirma un perfil.')
            self.store.get('profile',owner,profile_id)
        mime,content=inspect_document(data,self.settings.billing_max_upload_bytes)
        sha256=hashlib.sha256(content).hexdigest()
        # Telegram conoce el perfil antes de guardar la subida sin clasificar.
        # Reconocer los mismos bytes evita otro archivo, consentimiento y cargo.
        if purpose=='unclassified' and profile_id:
            existing=next((item for item in self.store.list('document',owner)
                if item['purpose']=='ticket' and item.get('profile_id')==profile_id and item['sha256']==sha256
                and (not item.get('superseded_by') or item.get('duplicate_ticket_id'))),None)
            if existing:
                return {'id':existing['id'],'purpose':'ticket','duplicate':True,'expires':existing['_expires']}
        return self._store_document(owner,purpose,mime,base64.b64encode(content).decode(),sha256,profile_id)

    def _store_document(self,owner,purpose,mime,encoded,sha256,profile_id,*,source=None,lookup_suffix=''):
        lookup=self.store.digest(f'{purpose}:{profile_id or ""}:'+sha256+lookup_suffix)
        existing=self.store.find('document',owner,lookup)
        if existing:
            return {'id':existing['id'],'purpose':purpose,'duplicate':True,'expires':existing['_expires']}
        payload={'purpose':purpose,'mime':mime,'data':encoded,'profile_id':profile_id,
            'sha256':sha256,'analysis_consent':False,'extracted':None}
        retention=self.settings.billing_ticket_retention_hours if purpose=='ticket' else self.settings.billing_document_retention_hours
        deadline=time.time()+retention*3600
        if source and source['purpose']!='unclassified' and source['_expires']:
            deadline=min(deadline,source['_expires'])
        if source and source['purpose']=='unclassified':
            record=self.store.update('document',owner,source['id'],payload)
            if purpose=='ticket':
                record=self.store.expire_at('document',owner,record['id'],deadline)
            self.store.change_lookup('document',owner,source['id'],lookup)
        else:
            if self.store.count('document',owner)>=self.settings.billing_max_documents:
                raise AppError(429,'document_limit','Elimina documentos anteriores antes de subir más archivos.')
            record=self.store.add('document',owner,payload,lookup=lookup,
                expires=deadline)
        if purpose=='ticket':
            pending={key:None for key in TicketExtraction.model_fields}
            pending.update(profile_id=profile_id,document_id=record['id'],verified_email=None,status=Status.TICKET_RECIBIDO,
                status_history=[{'status':Status.TICKET_RECIBIDO,'at':time.time()}],delivery=None,
                certificate_requested=False,csf_supplement_sent=False,invoice_documents=[],validation=None,note='Pendiente de análisis autorizado.')
            pending=self.store.add('ticket',owner,pending,lookup=self.store.digest('document:'+record['id']))
            record['ticket_id']=pending['id']
            record=self.store.update('document',owner,record['id'],record)
        self.store.audit(owner,record['id'],'document_received')
        return {'id':record['id'],'purpose':purpose,'duplicate':False,'expires':record['_expires']}

    def classify_document(self,owner,identifier,purpose,*,profile_id=None):
        """Clasifica bytes ya validados; no vuelve a subirlos ni llama al modelo.

        Una corrección después de un análisis conserva el intento y crea otro
        documento tipado, sin reutilizar extracciones de un esquema diferente.
        """
        source=self.document(owner,identifier)
        if purpose not in {'csf','ticket'} or source['purpose'] not in {'unclassified','csf','ticket'}:
            raise AppError(422,'document_purpose','Solo puedes clasificar una constancia o un ticket propio.')
        if purpose=='ticket' and not profile_id:
            raise AppError(409,'fiscal_profile_required','El ticket queda pendiente hasta confirmar un perfil fiscal.')
        if profile_id:
            self.store.get('profile',owner,profile_id)
        result=self._store_document(owner,purpose,source['mime'],source['data'],source['sha256'],profile_id,source=source)
        target=self.document(owner,result['id'])
        if purpose=='ticket' and target.get('duplicate_ticket_id'):
            return result
        if target.get('superseded_by') or target.get('intended_purpose'):
            target.update(superseded_by=None,intended_purpose=None)
            self.store.update('document',owner,target['id'],target)
        if result['id']!=source['id']:
            source.update(superseded_by=result['id'],intended_purpose=None)
            self.store.update('document',owner,source['id'],source)
        return result

    async def _extract(self,owner,identifier,purpose,consent):
        self.require_enabled()
        document=self.document(owner,identifier)
        if document['purpose']!=purpose:
            raise AppError(422,'document_purpose','El archivo no corresponde a este paso.')
        if document.get('extracted') is not None:
            return document,document['extracted']
        if consent is not True:
            raise AppError(409,'analysis_consent','Autoriza expresamente el análisis del documento con OpenAI antes de continuar.')
        if self.analysis_lock.locked():
            raise AppError(429,'billing_busy','Hay otro documento en análisis. Espera unos segundos.')
        async with self.analysis_lock:
            day=datetime.now(timezone.utc).date().isoformat()
            with self.database.connection(True) as conn:
                used=conn.execute('SELECT COUNT(*) FROM billing_extractions WHERE owner=? AND day=?',(owner,day)).fetchone()[0]
                if used>=self.settings.billing_max_daily_extractions:
                    raise AppError(429,'billing_daily_limit','Alcanzaste el límite diario de análisis de documentos.')
                try:
                    conn.execute('INSERT INTO billing_extractions VALUES(?,?,?,?)',(owner,day,identifier,'started'))
                except sqlite3.IntegrityError:
                    raise AppError(409,'extraction_attempted','Este documento ya tuvo un intento de análisis. Consulta su estado; no se repetirá automáticamente.') from None
            document.update(analysis_consent=True,extraction_version=EXTRACTION_VERSION,analysis_error=None)
            document=self.store.update('document',owner,identifier,document)
            try:
                routing=purpose=='ticket' and document.get('analysis_scope') in {'portal-fields-v1','portal-fields-v2','portal-fields-v3'} and hasattr(self.extractor,'extract_routing')
                if routing:
                    result=await self.extractor.extract_routing(document['mime'],base64.b64decode(document['data']))
                    document['analysis_stage']='routing'
                else:
                    result=await self.extractor.extract(purpose,document['mime'],base64.b64decode(document['data']))
                schema=FiscalExtraction if purpose=='csf' else TicketExtraction
                extracted=schema.model_validate(result).model_dump()
                document['extracted']=extracted
                document=self.store.update('document',owner,identifier,document)
                with self.database.connection(True) as conn:
                    conn.execute("UPDATE billing_extractions SET status='complete' WHERE owner=? AND document_id=?",(owner,identifier))
                return document,extracted
            except Exception as error:
                with self.database.connection(True) as conn:
                    conn.execute("UPDATE billing_extractions SET status='failed' WHERE owner=? AND document_id=?",(owner,identifier))
                failure=error if isinstance(error,AppError) else AppError(422,'extraction_format','La respuesta de IA no tuvo el formato esperado. El archivo sigue guardado.')
                document['analysis_error']={'code':failure.code,'message':failure.message}
                self.store.update('document',owner,identifier,document)
                raise failure from None

    async def process_fiscal_certificate(self,owner,document_id,*,consent=False,replace_profile_id=None):
        document,data=await self._extract(owner,document_id,'csf',consent)
        if not data['is_fiscal_certificate']:
            raise AppError(422,'not_fiscal_certificate','El documento no se identificó como Constancia de Situación Fiscal.')
        if not data['readable']:
            raise AppError(422,'unreadable_certificate','La constancia no es legible. Envía una fotografía o PDF más claro.')
        if replace_profile_id:
            existing=self.store.get('profile',owner,replace_profile_id)
        else:
            existing=None
        fields={key:data[key] for key in ('rfc','name','postal_code','fiscal_regime')}
        if fields['rfc']:
            fields['rfc']=fields['rfc'].upper().strip()
        fields['fiscal_regime']=regime_code(fields['fiscal_regime'])
        draft=self.store.add('profile_draft',owner,fields|{'document_id':document['id'],
            'replace_profile_id':replace_profile_id,'replace_version':existing['_version'] if existing else None,
            'cfdi_use':existing.get('cfdi_use') if existing else None,'email':existing.get('email') if existing else None},expires=time.time()+3600)
        return public(draft)|{'complete':validate_fiscal(fields)}

    def draft_profile_update(self,owner,profile_id):
        """Copia un perfil propio a un borrador; no cambia el perfil confirmado."""
        self.require_enabled()
        profile=self.store.get('profile',owner,profile_id)
        fields={key:profile[key] for key in ('rfc','name','postal_code','fiscal_regime','cfdi_use','email','document_id')}
        return self.store.add('profile_draft',owner,fields|{'replace_profile_id':profile_id,
            'replace_version':profile['_version']},expires=time.time()+3600)

    def complete_fiscal_field(self,owner,draft_id,field,value):
        """Completa un dato pendiente con entrada humana; no confirma el perfil."""
        self.require_enabled()
        draft=self.store.get('profile_draft',owner,draft_id)
        if field not in missing_fiscal_fields(draft) or not isinstance(value,str):
            raise AppError(409,'fiscal_field','Ese campo no está pendiente. Escribe /perfil.')
        value=value.strip()
        if not value or len(value)>250 or any(ord(char)<32 for char in value):
            raise AppError(422,'fiscal_field','Escribe solo el dato tal como aparece en tu CSF.')
        if field=='rfc':
            value=value.upper()
        elif field=='fiscal_regime':
            value=regime_code(value)
        candidate=draft|{field:value}
        if field in missing_fiscal_fields(candidate):
            raise AppError(422,'fiscal_field','El dato no es válido. Revísalo en tu CSF.')
        result=self.store.update('profile_draft',owner,draft_id,candidate,version=draft['_version'])
        self.store.audit(owner,draft_id,'fiscal_field_completed_by_user')
        return result

    def extract_fiscal_profile(self,owner,draft_id):
        self.require_enabled()
        return public(self.store.get('profile_draft',owner,draft_id))

    def profile_options(self,owner,draft_id,cfdi_use,email):
        draft=self.store.get('profile_draft',owner,draft_id)
        if cfdi_use not in CFDI_USES:
            raise AppError(422,'cfdi_use','Selecciona expresamente un uso de CFDI del catálogo.')
        draft.update(cfdi_use=cfdi_use,email=normalized_email(email))
        return self.store.update('profile_draft',owner,draft_id,draft)

    def confirm_fiscal_profile(self,owner,draft_id,*,cfdi_use=None,email=None,confirmed=False):
        self.require_enabled()
        if confirmed is not True:
            raise AppError(409,'profile_confirmation','Revisa y confirma el perfil fiscal antes de guardarlo.')
        draft=self.store.get('profile_draft',owner,draft_id)
        if cfdi_use is not None or email is not None:
            draft=self.profile_options(owner,draft_id,cfdi_use,email)
        if not validate_fiscal(draft) or draft.get('cfdi_use') not in CFDI_USES or not draft.get('email'):
            raise AppError(422,'incomplete_fiscal_profile','Faltan datos fiscales legibles, el uso de CFDI o el correo autorizado.')
        fields={key:draft[key] for key in ('rfc','name','postal_code','fiscal_regime','cfdi_use','email','document_id')}
        if draft['replace_profile_id']:
            profile=self.store.update('profile',owner,draft['replace_profile_id'],fields,version=draft['replace_version'])
        else:
            if len(self.store.list('profile',owner))>=self.settings.billing_max_profiles:
                raise AppError(429,'profile_limit','Alcanzaste el límite de perfiles fiscales.')
            profile=self.store.add('profile',owner,fields)
        self.store.delete('profile_draft',owner,draft_id)
        self.store.audit(owner,profile['id'],'profile_confirmed')
        return public(profile)

    async def analyze_ticket(self,owner,document_id,*,consent=False):
        document=self.document(owner,document_id)
        if document['purpose']!='ticket' or not document['profile_id']:
            raise AppError(409,'fiscal_profile_required','Selecciona primero un perfil fiscal confirmado.')
        self.store.get('profile',owner,document['profile_id'])
        pending=self.resolve_ticket(owner,document['ticket_id'])
        if document.get('duplicate_ticket_id') or document.get('superseded_by') or pending['document_id']!=document_id:
            return public(pending)|{'duplicate':True,'channels':self.detect_billing_channels(owner,pending['id'])}
        if pending['status'] not in {Status.TICKET_RECIBIDO,Status.ERROR}:
            return public(pending)|{'duplicate':True,'channels':self.detect_billing_channels(owner,pending['id'])}
        try:
            document,data=await self._extract(owner,document_id,'ticket',consent)
        except AppError as error:
            pending['analysis_error']={'code':error.code,'message':error.message}
            self._status(owner,pending,Status.ERROR,error.message)
            raise
        qr_urls=receipt_qr_urls(document)
        if data.get('billing_url') and unrelated_billing_link(data['billing_url'],data.get('billing_url_evidence')):
            data['billing_url']=None
        if not data.get('billing_url') and len(qr_urls)==1 and re.search(r'factur|cfdi',data.get('billing_url_evidence') or '',re.I):
            if not unrelated_billing_link(qr_urls[0],data.get('billing_url_evidence')):data['billing_url']=qr_urls[0]
        if not data['is_ticket'] or (not data['readable'] and not any(data.get(key) for key in ('merchant','folio','total','billing_url','billing_email'))):
            self._status(owner,pending,Status.REQUIERE_INTERVENCION,'No se identificaron datos de compra. El archivo sigue guardado.')
            raise AppError(422,'unreadable_ticket','No pude identificar datos de compra en este archivo. Revisa el tipo o la foto.')
        complete=not missing_receipt_fields(data)
        merchant=(data.get('issuer_rfc') or data.get('merchant') or '').strip().upper()
        amount=format(Decimal(data['total']).normalize(),'f') if data.get('total') else ''
        reference_key,_,reference=purchase_reference(data)
        identity_reference=(reference or '').strip().upper() if reference_key=='folio' else (reference_key or '')+':'+(reference or '').strip()
        identity='|'.join([merchant,data.get('purchase_date') or '',identity_reference,amount])
        lookup=self.store.digest(identity if complete else document['sha256'])
        duplicate=self.store.find('ticket',owner,lookup)
        if duplicate and duplicate['id']!=pending['id']:
            if duplicate.get('superseded_by')==pending['id'] and not duplicate.get('delivery'):
                self.store.change_lookup('ticket',owner,duplicate['id'],self.store.digest('superseded:'+duplicate['id']))
            else:
                duplicate=self.resolve_ticket(owner,duplicate['id'])
                # Conservar la foto recibida y dirigir sus botones a la solicitud
                # existente; el registro vacío provisional no es otra solicitud.
                document.update(ticket_id=duplicate['id'],duplicate_ticket_id=duplicate['id'],
                    superseded_by=duplicate['document_id'])
                self.store.update('document',owner,document_id,document)
                self.store.delete('ticket',owner,pending['id'])
                self.store.audit(owner,document_id,'duplicate_ticket_linked')
                return public(duplicate)|{'duplicate':True,'channels':self.detect_billing_channels(owner,duplicate['id'])}
        self.store.change_lookup('ticket',owner,pending['id'],lookup)
        contact=self.contacts.get(data.get('issuer_rfc'))
        if contact:
            data.update(billing_email=contact['email'],email_explicitly_for_billing=True,
                billing_email_evidence='Fuente oficial registrada: '+contact['official_url'])
        ticket=data|{'profile_id':document['profile_id'],'document_id':document_id,'verified_email':None,
            'status':Status.DATOS_EXTRAIDOS if complete else Status.REQUIERE_INTERVENCION,
            'status_history':pending['status_history'],
            'delivery':None,'certificate_requested':False,'csf_supplement_sent':False,'invoice_documents':[],
            'qr_urls':qr_urls,'validation':None,'note':'' if complete else 'Falta completar: '+', '.join(missing_receipt_fields(data))+'.'}
        if document.get('analysis_stage')=='routing' and data.get('billing_url'):
            ticket.update(portal_driven=True,portal_stage='awaiting_form',note='Portal identificado; falta leer su formulario.')
        if document.get('analysis_scope')=='portal-fields-v3':ticket['receipt_summary_read']=True
        ticket=self.store.update('ticket',owner,pending['id'],ticket)
        ticket=self._status(owner,ticket,ticket['status'])
        if document.get('analysis_stage')=='routing' and not data.get('billing_url') and data.get('email_explicitly_for_billing'):
            await self.portal_preparation.read_for_email(owner,ticket['id'])
            ticket=self.resolve_ticket(owner,ticket['id'])
        if self.portal_memory.suggestion(owner,ticket):
            ticket=self.portal_memory.select(owner,ticket['id'])
        if document.get('analysis_scope')=='portal-fields-v3' and not ticket.get('billing_url'):
            ticket=await self.portal_discovery.find(owner,ticket['id'])
        return public(ticket)|{'channels':self.detect_billing_channels(owner,ticket['id'])}

    async def inspect_billing_portal(self,owner,ticket_id):
        self.require_enabled()
        ticket=self.store.get('ticket',owner,ticket_id)
        if not ticket.get('billing_url'):
            raise AppError(409,'billing_portal','No hay una página de facturación identificada.')
        self.portal_permissions.require(owner,ticket)
        inspection=await self.portal_inspector.inspect(self.portal_memory.entry_url(owner,ticket))
        profile=self.store.get('profile',owner,ticket['profile_id'])
        ticket['portal_inspection']=portal_plan(ticket|profile,inspection)
        ticket=self.store.update('ticket',owner,ticket_id,ticket)
        self.store.audit(owner,ticket_id,'portal_public_form_inspected')
        return ticket

    def browser_review(self,owner,ticket_id):
        ticket=self.resolve_ticket(owner,ticket_id)
        record=self.store.find('browser_review',owner,self.store.digest('browser:'+ticket_id))
        if not record or record.get('scope')!=self.portal_permissions.scope(ticket):
            raise AppError(404,'browser_session','La revisión venció. Inicia otra revisión.')
        return record

    def portal_resume_url(self,owner,ticket):
        form=self.portal_form_url(owner,ticket)
        if form:return form
        saved=ticket.get('portal_checkpoint') or {}
        url=saved.get('url')
        if (not url or saved.get('expires',0)<=time.time() or saved.get('scope')!=self.portal_permissions.scope(ticket)
                or not self.portal_permissions.route_allowed(owner,ticket) or bookmark_url(url)!=url):
            return None
        return url

    def portal_form_url(self,owner,ticket):
        saved=ticket.get('portal_form_checkpoint') or {}
        url=saved.get('url')
        if (not url or saved.get('expires',0)<=time.time() or saved.get('scope')!=self.portal_permissions.scope(ticket)
                or not self.portal_permissions.route_allowed(owner,ticket) or bookmark_url(url)!=url):
            return None
        return url

    def _save_portal_checkpoint(self,owner,ticket,snapshot,brand_label):
        form=stable_form_bookmark(ticket,snapshot)
        if form and self.portal_permissions.route_allowed(owner,ticket):
            ticket['portal_form_checkpoint']={'url':form,'scope':self.portal_permissions.scope(ticket),
                'brand_label':brand_label,'expires':time.time()+self.settings.billing_ticket_retention_hours*3600}
        url=snapshot.get('bookmark_url')
        if url and bookmark_url(url)==url and not snapshot['captcha'] and not snapshot.get('login_required') and self.portal_permissions.route_allowed(owner,ticket):
            ticket['portal_checkpoint']={'url':url,'scope':self.portal_permissions.scope(ticket),
                'brand_label':brand_label,'expires':time.time()+1800}

    async def browse_portal(self,owner,ticket_id,*,observation_id=None,reference=None,restart_exhausted=False):
        self.require_enabled()
        ticket=self.resolve_ticket(owner,ticket_id)
        ticket_id=ticket['id']
        tx=self.portal_submission.current(owner,ticket_id)
        if tx and tx.get('private'):
            raise AppError(409,'portal_private_step','Continúa el formulario privado desde /estado. No se reiniciará su sesión.')
        if not ticket.get('billing_url'):
            raise AppError(409,'billing_portal','Primero se necesita una página de facturación identificada.')
        self.portal_permissions.require(owner,ticket)
        starting_scope=self.portal_permissions.scope(ticket)
        selected_action=None
        route_options={'public_route':True} if self.portal_permissions.route_allowed(owner,ticket) else {}
        lookup=self.store.digest('browser:'+ticket_id)
        previous=self.store.find('browser_review',owner,lookup)
        brand_label=(previous or {}).get('brand_label')
        if previous and previous.get('scope')!=self.portal_permissions.scope(ticket):
            await self.sync_portal_permissions(owner,ticket_id,cancel=True)
            previous=None
            brand_label=None

        async def open_current():
            nonlocal brand_label
            resume=self.portal_resume_url(owner,ticket)
            if resume:
                saved=ticket['portal_form_checkpoint'] if self.portal_form_url(owner,ticket)==resume else ticket['portal_checkpoint']
                brand_label=saved.get('brand_label')
            else:
                brand_label=None
            allowed=[portal_origin(resume)] if resume else self.portal_permissions.allowed(owner,ticket)
            return await self.browser.open(owner,resume or self.portal_memory.entry_url(owner,ticket),allowed=allowed,**route_options)

        async def discard_exhausted(snapshot):
            nonlocal previous
            if not restart_exhausted or not previous or reference:
                return False
            if (not snapshot['network_blocked'] or snapshot['captcha'] or snapshot.get('login_required')
                    or snapshot.get('block_reason') not in RESUMABLE_LIMITS):
                return False
            self._save_portal_checkpoint(owner,ticket,snapshot,brand_label)
            if not self.portal_resume_url(owner,ticket):
                return False
            try:
                await self.browser.close_session(owner,snapshot['session_id'])
            except AppError as error:
                if error.code!='browser_session':
                    raise
            self.store.delete('browser_review',owner,previous['id'])
            previous=None
            return True

        if not previous:
            self.portal_permissions._expire_lookup('browser_review',owner,lookup)
        try:
            if previous:
                await discard_exhausted(previous['snapshot'])
            if reference:
                if not previous or previous['snapshot']['observation_id']!=observation_id or reference not in {item['reference'] for item in previous['snapshot']['actions']}:
                    raise AppError(409,'browser_stale','La página cambió. Actualiza la revisión.')
                action=next(item for item in previous['snapshot']['actions'] if item['reference']==reference)
                selected_action=action
                if action.get('brand'):
                    brand_label=action['label']
                if action.get('permission_origin'):
                    self.portal_permissions.require(owner,ticket,action['permission_origin'])
                if previous['snapshot']['captcha'] or previous['snapshot']['network_blocked'] or previous['snapshot'].get('login_required'):
                    raise AppError(409,'browser_intervention','La página requiere intervención humana antes de continuar.')
                snapshot=await self.browser.act(owner,previous['snapshot']['session_id'],observation_id,reference)
            elif previous:
                try:
                    snapshot=await self.browser.observe(owner,previous['snapshot']['session_id'])
                except AppError as error:
                    if error.code!='browser_session':
                        raise
                    # El reinicio pierde contextos efímeros. La apertura pública
                    # sigue exigiendo el permiso vigente del mismo usuario/ticket.
                    self.store.delete('browser_review',owner,previous['id'])
                    previous=None
                    snapshot=await open_current()
            else:
                snapshot=await open_current()
            # El presupuesto pudo agotarse después de la observación guardada.
            # Solo el primer observe de un trabajo nuevo puede reabrir una vez.
            if await discard_exhausted(snapshot):
                snapshot=await open_current()
        except AppError as error:
            ticket=self.resolve_ticket(owner,ticket_id)
            ticket['portal_browser_error']={'code':error.code,'message':error.message}
            self.store.update('ticket',owner,ticket_id,ticket)
            if error.code=='browser_session' and previous:
                self.store.delete('browser_review',owner,previous['id'])
            raise
        ticket=self.resolve_ticket(owner,ticket_id)
        if self.portal_permissions.scope(ticket)!=starting_scope:
            raise AppError(409,'portal_changed','Cambió el perfil o portal durante la revisión. Revisa el ticket.')
        if selected_action:
            self.portal_preparation.bind_alternative(ticket,previous['snapshot'],snapshot,selected_action)
        fields=[]
        for item in self.portal_preparation.active_fields(ticket,snapshot):
            fields.append({'key':item.get('key'),'label':item['label'],'required':item['required'],
                'matched':bool(ticket.get(item.get('key'))),'options':item['options']})
        ticket['portal_browser']={'page':snapshot['page'],'fields':fields,'checked_at':time.time(),
            'purchase_validated':False,'submitted':False,'diagnostics':snapshot.get('diagnostics'),
            'block_reason':snapshot.get('block_reason'),'omitted_writes':snapshot.get('omitted_writes',0),
            'partial':snapshot.get('partial',False)}
        if ticket.get('portal_stage')=='awaiting_form' and public_form_ready(snapshot):
            ticket['portal_stage']='fields_pending'
        self._save_portal_checkpoint(owner,ticket,snapshot,brand_label)
        ticket['portal_browser_error']=None
        self.store.update('ticket',owner,ticket_id,ticket)
        payload={'ticket_id':ticket_id,'scope':self.portal_permissions.scope(ticket),'snapshot':snapshot,'brand_label':brand_label}
        if previous:
            record=self.store.update('browser_review',owner,previous['id'],payload)
        else:
            record=self.store.add('browser_review',owner,payload,lookup=lookup,expires=time.time()+150)
        self.store.audit(owner,ticket_id,'public_browser_observed')
        return record

    async def sync_portal_permissions(self,owner,ticket_id,*,cancel=False):
        ticket=self.resolve_ticket(owner,ticket_id)
        record=self.store.find('browser_review',owner,self.store.digest('browser:'+ticket['id']))
        if not record:
            return
        if cancel:
            try:
                await self.browser.close_session(owner,record['snapshot']['session_id'])
            except AppError:
                pass
            self.store.delete('browser_review',owner,record['id'])
            return
        try:
            route_options={'public_route':True} if self.portal_permissions.route_allowed(owner,ticket) else {}
            snapshot=await self.browser.allow(owner,record['snapshot']['session_id'],self.portal_permissions.allowed(owner,ticket),**route_options)
        except AppError as error:
            if error.code=='browser_session':
                self.store.delete('browser_review',owner,record['id'])
                return
            raise
        record['snapshot']=snapshot
        self.store.update('browser_review',owner,record['id'],record)
        if (ticket.get('portal_browser_error') or {}).get('code')=='portal_permission':
            ticket['portal_browser_error']=None
            self.store.update('ticket',owner,ticket['id'],ticket)

    def _status(self,owner,ticket,status,note=''):
        ticket['status']=status
        ticket['note']=note
        ticket.setdefault('status_history',[]).append({'status':status,'at':time.time()})
        ticket=self.store.update('ticket',owner,ticket['id'],ticket)
        self.store.audit(owner,ticket['id'],str(status))
        return ticket

    def retry_ticket_after_update(self,owner,document_id):
        source=self.document(owner,document_id)
        ticket=self.store.get('ticket',owner,source.get('ticket_id',''))
        tx=self.portal_submission.current(owner,ticket['id'])
        if tx and (tx.get('private') or tx['state'] in {'unknown','submitted'}):
            raise AppError(409,'analysis_retry','El formulario del portal está en curso. Continúa desde /estado.')
        needs_reference=bool(set(pending_receipt_fields(ticket)) & {'account_number','reference_number','billing_code','web_id','toll_station','lane','purchase_time'}) or bool(browser_missing_fields(ticket))
        if source['purpose']!='ticket' or source.get('superseded_by') or ticket.get('delivery') or (ticket['status'] not in {Status.ERROR,Status.REQUIERE_INTERVENCION} and not needs_reference):
            raise AppError(409,'analysis_retry','Este archivo ya se procesó. Consulta /tickets.')
        if any(item['ticket_id']==ticket['id'] and item['_state'] in {'pending','claimed','unknown'} for item in self.store.list('email_approval',owner)):
            raise AppError(409,'analysis_retry','Revisa el correo pendiente antes de cambiar el ticket.')
        if source.get('extraction_version')==EXTRACTION_VERSION:
            raise AppError(409,'analysis_retry','Ya se intentó con esta versión. No se repetirá automáticamente.')
        result=self._store_document(owner,'ticket',source['mime'],source['data'],source['sha256'],source['profile_id'],
            source=source,lookup_suffix=':retry:'+EXTRACTION_VERSION)
        source['superseded_by']=result['id']
        self.store.update('document',owner,source['id'],source)
        ticket['superseded_by']=self.document(owner,result['id'])['ticket_id']
        self._status(owner,ticket,Status.REQUIERE_INTERVENCION,'Archivo recuperado para una nueva lectura autorizada.')
        return result

    def complete_ticket_field(self,owner,ticket_id,field,value,*,correction=False):
        ticket=self.store.get('ticket',owner,ticket_id)
        tx=self.portal_submission.current(owner,ticket_id)
        if tx and tx['state'] in {'unknown','submitted'}:
            raise AppError(409,'already_requested','Hay un envío al portal registrado. Revisa su estado antes de cambiar los datos.')
        try:
            document=self.document(owner,ticket['document_id'])
            analyzed=(document.get('extracted') or {}).get('is_ticket') or (document.get('portal_replacement') and ticket.get('is_ticket') is True)
        except AppError as error:
            if error.status!=404:
                raise
            # Los datos de un ticket ya analizado sobreviven al adjunto temporal.
            analyzed=ticket.get('is_ticket') is True
        allowed=set(pending_receipt_fields(ticket))|set(browser_missing_fields(ticket))
        if correction:
            allowed={item['key'] for item in (ticket.get('portal_browser') or {}).get('fields',[])} & {
                'account_number','folio','reference_number','billing_code','web_id','store_number','toll_station','lane','purchase_time','purchase_date','total'}
        if ticket.get('delivery') or not analyzed or field not in allowed:
            raise AppError(409,'ticket_field','Ese dato no está pendiente. Consulta /tickets.')
        value=value.strip()
        if not value or len(value)>160 or any(ord(char)<32 for char in value):
            raise AppError(422,'ticket_field','Escribe solo el dato visible en el ticket.')
        if field=='purchase_date':
            value=purchase_date(value)
        elif field=='total':
            value=purchase_total(value)
        elif field in {'folio','account_number','store_number'} and len(value)>100:
            value=None
        elif field=='lane' and len(value)>40 or field=='purchase_time' and len(value)>20:
            value=None
        if not value:
            raise AppError(422,'ticket_field','El dato no tiene un formato válido.')
        ticket[field]=value
        ticket['portal_data_error']=None
        if ticket.get('portal_inspection'):
            profile=self.store.get('profile',owner,ticket['profile_id'])
            ticket['portal_inspection']=portal_plan(ticket|profile,ticket['portal_inspection'])
        ticket['fields_completed_by_user']=list(dict.fromkeys([*ticket.get('fields_completed_by_user',[]),field]))
        self.store.audit(owner,ticket_id,'ticket_field_completed_by_user')
        complete=not missing_receipt_fields(ticket)
        return self._status(owner,ticket,Status.DATOS_EXTRAIDOS if complete else Status.REQUIERE_INTERVENCION,
            '' if complete else 'Hay datos de compra pendientes.')

    def detect_billing_channels(self,owner,ticket_id):
        self.require_enabled()
        ticket=self.store.get('ticket',owner,ticket_id)
        # Los adaptadores proceden exclusivamente de configuración del servidor.
        for adapter in self.portals:
            if adapter.authorized and adapter.supports(ticket):
                return {'method':'portal','adapter':adapter.name,'authorized':True,
                    'reason':'Hay un portal o API compatible, verificado y autorizado.'}
        if ticket.get('billing_url'):
            return {'method':'portal_manual','url':ticket['billing_url'],'authorized':False,
                'reason':'Página de facturación identificada. No hay automatización instalada para este portal.'}
        if ticket.get('billing_email') and ticket.get('email_explicitly_for_billing'):
            return {'method':'email','verified':bool(ticket.get('verified_email')),
                'official_source':(self.contacts.get(ticket.get('issuer_rfc')) or {}).get('official_url'),
                'reason':'Correo señalado para facturación; requiere verificación y confirmación antes de enviar.'}
        return {'method':'manual','reason':'No hay un portal compatible autorizado ni un correo identificado expresamente para facturación.'}

    def verify_billing_email(self,owner,ticket_id,*,confirmed=False):
        ticket=self.store.get('ticket',owner,ticket_id)
        evidence=ticket.get('billing_email_evidence') or ''
        email=normalized_email(ticket.get('billing_email') or '')
        label=re.sub(re.escape(email),'',evidence,flags=re.I)
        official=self.contacts.get(ticket.get('issuer_rfc'))
        official_match=bool(official and official['email']==email)
        ticket_match=bool(ticket.get('email_explicitly_for_billing') and email in evidence.lower() and re.search(r'\bfactur(?:as?|aci[oó]n|ar)\b|\bcfdi\b',label,re.I))
        if confirmed is not True or not (official_match or ticket_match):
            raise AppError(409,'unverified_billing_email','Revisa el ticket original: debe señalar expresamente este correo para facturación. Un correo genérico no está autorizado.')
        ticket['verified_email']={'email':email,'source':'official_source_reviewed_by_user' if official_match else 'ticket_reviewed_by_user','verified_at':time.time(),
            'document_id':ticket['document_id']}
        self.store.audit(owner,ticket_id,'billing_email_verified_by_user')
        return public(self.store.update('ticket',owner,ticket_id,ticket))

    def prepare_invoice_request_email(self,owner,ticket_id,*,include_csf=False,authorize_csf=False,user_requested_csf=False):
        self.require_enabled()
        ticket=self.store.get('ticket',owner,ticket_id)
        tx=self.portal_submission.current(owner,ticket_id)
        if ticket.get('portal_delivery') or tx and tx['state'] in {'unknown','submitted'}:
            raise AppError(409,'already_requested','Hay un envío al portal registrado. Consulta su estado antes de usar otro canal.')
        profile=self.store.get('profile',owner,ticket['profile_id'])
        if self.detect_billing_channels(owner,ticket_id)['method']=='portal':
            raise AppError(409,'portal_has_priority','Existe un portal compatible autorizado; utiliza primero ese canal.')
        verified=ticket.get('verified_email')
        if not verified:
            raise AppError(409,'unverified_billing_email','Verifica expresamente el correo de facturación antes de preparar el envío.')
        if missing_receipt_fields(ticket):
            raise AppError(422,'incomplete_ticket','Faltan datos legibles de la compra.')
        supplement=bool(ticket.get('delivery'))
        if supplement and (not include_csf or not ticket.get('certificate_requested') or ticket.get('csf_supplement_sent')):
            raise AppError(409,'already_requested','La solicitud ya fue enviada; no se enviará otra automáticamente.')
        attachments=[self.document(owner,ticket['document_id'])]
        if include_csf:
            if authorize_csf is not True or not (ticket.get('certificate_requested') or user_requested_csf is True):
                raise AppError(409,'csf_authorization','La constancia necesita petición del comercio o elección explícita del usuario, y autorización específica.')
            try:
                attachments.append(self.document(owner,profile['document_id']))
            except AppError as error:
                if error.status!=404:
                    raise
                raise AppError(409,'csf_expired','La copia de tu CSF venció. Actualízala para adjuntarla; el perfil sigue guardado.') from None
        if sum(len(base64.b64decode(item['data'])) for item in attachments)>6_000_000:
            raise AppError(413,'email_attachment_size','Los adjuntos de un correo deben sumar como máximo 6 MB. Reduce el tamaño de los archivos antes de preparar el envío.')
        _,reference_label,reference=purchase_reference(ticket)
        subject='Solicitud de factura CFDI 4.0 — '+reference_label+' '+re.sub(r'[\r\n\x00-\x1f]',' ',reference)
        references='\n'.join(label+': '+str(ticket[key]) for key,label in [('account_number','Cuenta'),('folio','Folio'),('reference_number','Referencia'),('billing_code','Código de facturación')] if ticket.get(key))
        body=f'''Buen día,

Solicito su apoyo con la emisión de la factura correspondiente al ticket adjunto.

Datos de la compra:
Establecimiento: {ticket['merchant']}
Fecha: {ticket['purchase_date']}
{references}
Importe: {ticket['total']} {ticket.get('currency') or ''}

Datos fiscales:
RFC: {profile['rfc']}
Nombre o razón social: {profile['name']}
Código postal fiscal: {profile['postal_code']}
Régimen fiscal: {profile['fiscal_regime']}
Uso de CFDI: {profile['cfdi_use']}

Agradeceré que los archivos XML y PDF sean enviados a {profile['email']}.

Quedo atento a su confirmación.
Saludos.'''
        payload={'ticket_id':ticket_id,'profile_id':profile['id'],'profile_version':profile['_version'],
            'purchase_fingerprint':self._purchase_fingerprint(ticket),
            'to':verified['email'],'subject':subject,'body':body,'fiscal_data':{key:profile[key] for key in ('rfc','name','postal_code','fiscal_regime','cfdi_use','email')},
            'attachments':[{'id':item['id'],'sha256':item['sha256'],'mime':item['mime'],
                'label':'Constancia de Situación Fiscal' if item['purpose']=='csf' else 'Ticket de compra'} for item in attachments],
            'include_csf':include_csf,'csf_share_reason':('user_requested' if user_requested_csf else 'merchant_requested') if include_csf else None,
            'supplement':supplement,'rfc_message_id':'<'+uuid.uuid4().hex+'@agente.invalid>'}
        account=self.mail.account(owner)
        if account:
            payload['rfc_message_id']='<'+uuid.uuid4().hex+'@'+normalized_email(account['email']).split('@',1)[1]+'>'
        self._status(owner,ticket,Status.CORREO_PREPARADO)
        return payload

    def request_email_send_approval(self,owner,ticket_id,*,include_csf=False,authorize_csf=False,user_requested_csf=False):
        for existing in self.store.list('email_approval',owner):
            if existing['ticket_id']==ticket_id and existing['_state'] in {'claimed','unknown'}:
                raise AppError(409,'mail_delivery_unknown','Hay un envío con resultado incierto. Revísalo manualmente antes de repetir cualquier acción.')
            if existing['ticket_id']==ticket_id and existing['_state']=='pending':
                if bool(existing['include_csf'])!=bool(include_csf):
                    raise AppError(409,'pending_email_approval','Rechaza primero el borrador anterior antes de cambiar sus adjuntos.')
                return public(existing)|{'expires':existing['_expires']}
        payload=self.prepare_invoice_request_email(owner,ticket_id,include_csf=include_csf,authorize_csf=authorize_csf,user_requested_csf=user_requested_csf)
        approval=self.store.add('email_approval',owner,payload,expires=time.time()+self.settings.approval_ttl_seconds,state='pending')
        ticket=self.store.get('ticket',owner,ticket_id)
        ticket['approval_id']=approval['id']
        self._status(owner,ticket,Status.PENDIENTE_CONFIRMACION)
        return public(approval)|{'expires':approval['_expires']}

    def _purchase_fingerprint(self,ticket):
        keys=('merchant','purchase_date','account_number','folio','reference_number','billing_code','total','currency',
            'web_id','toll_station','lane','purchase_time')
        return self.store.digest(json.dumps({key:ticket.get(key) for key in keys},sort_keys=True,ensure_ascii=False))

    async def send_invoice_request_email(self,owner,approval_id,*,confirmed=False):
        self.require_enabled()
        if confirmed is not True:
            raise AppError(409,'email_confirmation','Confirma expresamente el destinatario, los datos, el mensaje y los adjuntos.')
        approval=self.store.get('email_approval',owner,approval_id)
        ticket=self.store.get('ticket',owner,approval['ticket_id'])
        tx=self.portal_submission.current(owner,ticket['id'])
        if ticket.get('portal_delivery') or tx and tx['state'] in {'unknown','submitted'}:
            raise AppError(409,'already_requested','Hay un envío al portal registrado. No se duplicará por correo.')
        profile=self.store.get('profile',owner,approval['profile_id'])
        if approval.get('purchase_fingerprint') and approval['purchase_fingerprint']!=self._purchase_fingerprint(ticket):
            raise AppError(409,'approval_changed','Los datos de compra cambiaron. Revisa un correo nuevo.')
        if profile['_version']!=approval['profile_version'] or (ticket.get('verified_email') or {}).get('email')!=approval['to']:
            raise AppError(409,'approval_changed','Los datos cambiaron. Prepara y revisa una solicitud nueva.')
        documents=[self.document(owner,item['id']) for item in approval['attachments']]
        if any(document['sha256']!=preview['sha256'] for document,preview in zip(documents,approval['attachments'])):
            raise AppError(409,'attachment_changed','Los adjuntos cambiaron. Revisa una solicitud nueva.')
        if not self.settings.billing_live_email_enabled or not self.mail.ready(owner):
            raise AppError(503,'email_disabled','El envío real está desactivado o falta conectar Gmail. No se envió ningún correo.')
        if ticket.get('delivery') and (not approval['supplement'] or ticket.get('csf_supplement_sent')):
            raise AppError(409,'already_requested','La solicitud ya fue enviada.')
        self.store.claim('email_approval',owner,approval_id)
        message=EmailMessage()
        message['From']=normalized_email(self.mail.account(owner)['email'])
        message['To']=approval['to']
        message['Subject']=approval['subject']
        message['Message-ID']=approval['rfc_message_id']
        message['Reply-To']=normalized_email(self.mail.account(owner)['email'])
        if approval['supplement']:
            message['In-Reply-To']=ticket['delivery']['rfc_message_id']
            message['References']=ticket['delivery']['rfc_message_id']
        message.set_content(approval['body'])
        for document in documents:
            extension='pdf' if document['mime']=='application/pdf' else 'jpg'
            main,sub=document['mime'].split('/',1)
            message.add_attachment(base64.b64decode(document['data']),maintype=main,subtype=sub,
                filename=('constancia' if document['purpose']=='csf' else 'ticket')+'.'+extension)
        try:
            async with asyncio.timeout(25):
                delivery=await self.mail.send(owner,message,thread_id=ticket['delivery']['thread_id'] if approval['supplement'] else None)
            previous=(ticket.get('delivery') or {}).get('request_message_ids',[])
            ticket['delivery']={'message_id':delivery.message_id,'thread_id':delivery.thread_id,
                'request_message_ids':[*previous,approval['rfc_message_id']],
                'fiscal_data':approval['fiscal_data'],
                'rfc_message_id':approval['rfc_message_id'],'recipient':approval['to'],'sent_at':time.time()}
            ticket['csf_supplement_sent']=bool(approval['include_csf']) or ticket.get('csf_supplement_sent',False)
            self.store.update('email_approval',owner,approval_id,approval,state='used')
            ticket=self._status(owner,ticket,Status.CORREO_ENVIADO)
            ticket=self._status(owner,ticket,Status.ESPERANDO_RESPUESTA,'Solicitud enviada; todavía no es una factura emitida.')
            return public(ticket)
        except (Exception,asyncio.CancelledError):
            self.store.update('email_approval',owner,approval_id,approval,state='unknown')
            self._status(owner,ticket,Status.REQUIERE_INTERVENCION,'No se pudo confirmar el envío. Revisa Gmail antes de repetir: el correo podría haberse enviado.')
            raise AppError(502,'mail_delivery_unknown','El resultado del envío es incierto. No se reintentará automáticamente.') from None

    def reject_email(self,owner,approval_id):
        approval=self.store.get('email_approval',owner,approval_id)
        if approval['_state']!='pending':
            raise AppError(409,'billing_approval','La autorización ya fue utilizada.')
        self.store.update('email_approval',owner,approval_id,approval,state='rejected')
        ticket=self.store.get('ticket',owner,approval['ticket_id'])
        return public(self._status(owner,ticket,Status.CORREO_PREPARADO,'Envío rechazado; no se ejecutó.'))

    def mark_certificate_required(self,owner,ticket_id,*,confirmed=False,evidence=''):
        if confirmed is not True or not 10<=len(evidence.strip())<=1000:
            raise AppError(409,'merchant_csf_request','Confirma y describe la petición expresa de constancia completa del comercio.')
        ticket=self.store.get('ticket',owner,ticket_id)
        ticket.update(certificate_requested=True,certificate_request_evidence=evidence)
        return public(self.store.update('ticket',owner,ticket_id,ticket))

    async def track_invoice_email(self,owner,ticket_id,*,authorize_sat=False):
        ticket=self.store.get('ticket',owner,ticket_id)
        if not ticket.get('delivery'):
            raise AppError(409,'email_not_sent','Este ticket todavía no tiene un envío confirmado.')
        if not self.mail.ready(owner):
            raise AppError(503,'gmail_not_connected','Conecta tu buzón autorizado de Gmail.')
        try:
            async with asyncio.timeout(self.settings.run_timeout_seconds):
                replies=await self.mail.fetch(owner,ticket)
        except TimeoutError:
            raise AppError(504,'mailbox_timeout','La consulta del buzón tardó demasiado. No se repetirá automáticamente.') from None
        for reply in replies:
            try:
                await self.process_invoice_response(owner,ticket_id,reply,authorize_sat=False)
            except AppError:
                latest=self.store.get('ticket',owner,ticket_id)
                self._status(owner,latest,Status.REQUIERE_INTERVENCION,'La respuesta requiere revisión: su remitente, archivos o XML no pudieron validarse.')
                raise
        current=self.store.get('ticket',owner,ticket_id)
        if authorize_sat and current.get('validation') and not current['validation'].get('sat_verified'):
            return await self.validate_cfdi(owner,ticket_id,authorize_sat=True)
        return public(self.store.get('ticket',owner,ticket_id))

    def configure_tracking(self,owner,ticket_id,*,enabled,confirmed=False):
        if confirmed is not True:
            raise AppError(409,'tracking_confirmation','Confirma la política de consultas del buzón antes de activarla.')
        ticket=self.store.get('ticket',owner,ticket_id)
        if enabled:
            if not self.settings.billing_background_tracking_enabled or not self.mail.ready(owner) or not ticket.get('delivery'):
                raise AppError(503,'tracking_disabled','Falta habilitar el seguimiento en el servidor, conectar Gmail o confirmar el envío.')
            if ticket['status']==Status.FACTURACION_COMPLETADA:
                raise AppError(409,'tracking_complete','La solicitud ya está completada.')
            previous=ticket.get('tracking') or {}
            checks=previous.get('checks',0)
            if checks>=self.settings.billing_tracking_max_checks:
                raise AppError(429,'tracking_limit','Esta solicitud alcanzó su máximo de consultas automáticas. Continúa mediante revisión manual.')
            ticket['tracking']={'enabled':True,'checks':checks,'errors':0,
                'next_check':time.time()+self.settings.billing_tracking_interval_seconds,'authorized_at':time.time(),
                'notification_signature':self.store.digest(str((ticket['status'],len(ticket['invoice_documents']),ticket.get('merchant_csf_request_detected'),ticket.get('note'))))}
        else:
            ticket.setdefault('tracking',{})['enabled']=False
        return public(self.store.update('ticket',owner,ticket_id,ticket,state='tracking' if enabled else 'active'))

    async def poll_due(self):
        if not self.enabled or not self.settings.billing_background_tracking_enabled or self.poll_lock.locked():
            return 0
        count=0
        async with self.poll_lock:
            for owner,ticket in self.store.tracking_tickets():
                policy=ticket.get('tracking') or {}
                if not policy.get('enabled') or policy.get('next_check',0)>time.time():
                    continue
                if policy.get('checks',0)>=self.settings.billing_tracking_max_checks:
                    policy['enabled']=False
                    ticket['tracking']=policy
                    ticket=self._status(owner,ticket,Status.REQUIERE_INTERVENCION,'Se alcanzó el límite de consultas automáticas. Revisa la solicitud manualmente.')
                    self.store.update('ticket',owner,ticket['id'],ticket,state='active')
                    continue
                policy['checks']=policy.get('checks',0)+1
                policy['next_check']=time.time()+self.settings.billing_tracking_interval_seconds
                ticket['tracking']=policy
                self.store.update('ticket',owner,ticket['id'],ticket)
                count+=1
                try:
                    await self.track_invoice_email(owner,ticket['id'],authorize_sat=False)
                    current=self.store.get('ticket',owner,ticket['id'])
                    current['tracking']['errors']=0
                except AppError:
                    current=self.store.get('ticket',owner,ticket['id'])
                    current['tracking']['errors']=current['tracking'].get('errors',0)+1
                    if current['tracking']['errors']>=3:
                        current['tracking']['enabled']=False
                        current=self._status(owner,current,Status.REQUIERE_INTERVENCION,'El seguimiento se detuvo tras tres errores. Revisa la conexión del buzón.')
                if current['status']==Status.FACTURACION_COMPLETADA:
                    current['tracking']['enabled']=False
                signature=self.store.digest(str((current['status'],len(current['invoice_documents']),current.get('merchant_csf_request_detected'),current.get('note'))))
                changed=signature!=current['tracking'].get('notification_signature')
                current['tracking']['notification_signature']=signature
                self.store.update('ticket',owner,current['id'],current,state='tracking' if current['tracking']['enabled'] else 'active')
                if changed and self.notifier:
                    await self.notifier(owner,current)
        return count

    async def process_invoice_response(self,owner,ticket_id,reply,*,authorize_sat=False):
        ticket=self.store.get('ticket',owner,ticket_id)
        references=(ticket.get('delivery') or {}).get('request_message_ids',[(ticket.get('delivery') or {}).get('rfc_message_id')])
        if not ticket.get('delivery') or reply.in_reply_to not in references or normalized_email(reply.sender)!=ticket['delivery']['recipient']:
            raise AppError(409,'unrelated_reply','La respuesta no coincide con el destinatario y el hilo de esta solicitud.')
        lookup=self.store.digest(reply.identifier)
        if self.store.find('mail_reply',owner,lookup):
            return public(ticket)
        if len(reply.attachments)>6 or sum(len(item.data) for item in reply.attachments)>self.settings.billing_max_upload_bytes:
            raise AppError(413,'reply_size','Los adjuntos de la respuesta superan el límite.')
        if self.store.count('document',owner)+len(reply.attachments)>self.settings.billing_max_documents:
            raise AppError(429,'document_limit','Elimina archivos anteriores antes de guardar más adjuntos de factura.')
        profile=ticket['delivery'].get('fiscal_data') or self.store.get('profile',owner,ticket['profile_id'])
        validation=None
        expected_uuid=(ticket.get('validation') or {}).get('uuid')
        prepared=[]
        invoice_lookups=set()
        # Validar el conjunto antes de marcar el correo o persistir archivos:
        # un XML incorrecto no debe dejar un PDF huérfano ni impedir la revisión.
        for attachment in reply.attachments[:6]:
            mime,data=inspect_document(attachment.data,self.settings.billing_max_upload_bytes,allow_xml=True)
            if mime=='application/xml':
                validation=validate_cfdi(data,profile,ticket)
                if expected_uuid and expected_uuid!=validation['uuid']:
                    raise AppError(409,'conflicting_invoice','Hay más de un UUID para esta compra. Revisa los documentos antes de continuar.')
                expected_uuid=validation['uuid']
                invoice_lookup=self.store.digest(validation['uuid'])
                duplicate=self.store.find('invoice_uuid',owner,invoice_lookup)
                if duplicate and duplicate['ticket_id']!=ticket_id:
                    raise AppError(409,'duplicate_invoice','El mismo UUID ya está asociado a otro ticket.')
                if not duplicate:
                    invoice_lookups.add(invoice_lookup)
            elif mime!='application/pdf':
                continue
            prepared.append((mime,data))
        self.store.add('mail_reply',owner,{'ticket_id':ticket_id,'text':reply.text[:8000],'message_id':reply.identifier},lookup=lookup)
        if re.search(r'constancia|situaci[oó]n fiscal',reply.text,re.I):
            ticket['merchant_csf_request_detected']=True
        for invoice_lookup in invoice_lookups:
            self.store.add('invoice_uuid',owner,{'ticket_id':ticket_id},lookup=invoice_lookup)
        for mime,data in prepared:
            document=self.store.add('document',owner,{'purpose':'invoice_xml' if mime=='application/xml' else 'invoice_pdf',
                'mime':mime,'data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest(),
                'ticket_id':ticket_id,'profile_id':ticket['profile_id']},
                expires=time.time()+self.settings.billing_invoice_retention_days*86400)
            ticket['invoice_documents'].append(document['id'])
        if validation:
            ticket['validation']=validation
            ticket=self._status(owner,ticket,Status.FACTURA_RECIBIDA,'XML y datos del receptor cotejados; falta comprobar vigencia ante el SAT.')
        elif not ticket.get('validation'):
            return public(self._status(owner,ticket,Status.REQUIERE_INTERVENCION,
                'La respuesta no contiene un XML CFDI válido. Un PDF por sí solo no lo acredita.'))
        else:
            ticket=self.store.update('ticket',owner,ticket_id,ticket)
        if authorize_sat:
            return await self.validate_cfdi(owner,ticket_id,authorize_sat=True)
        if ticket.get('validation') and ticket['validation'].get('sat_verified'):
            return self._finish_verified_invoice(owner,ticket)
        return public(self._status(owner,ticket,Status.REQUIERE_INTERVENCION,'Revisa los archivos y autoriza la consulta de vigencia ante el SAT.'))

    def _finish_verified_invoice(self,owner,ticket):
        types={self.document(owner,identifier)['mime'] for identifier in ticket['invoice_documents']}
        if {'application/xml','application/pdf'}.issubset(types):
            ticket=self._status(owner,ticket,Status.FACTURACION_COMPLETADA,'PDF y XML disponibles; datos cotejados y vigencia SAT confirmada.')
        return public(ticket)

    async def validate_cfdi(self,owner,ticket_id,*,authorize_sat=False):
        ticket=self.store.get('ticket',owner,ticket_id)
        if not ticket.get('validation'):
            raise AppError(409,'xml_required','Primero se necesita un XML compatible con el ticket y el perfil.')
        if authorize_sat is not True:
            raise AppError(409,'sat_consent','Autoriza compartir RFC, UUID e importe con el SAT para consultar la vigencia.')
        if not self.settings.billing_sat_enabled:
            raise AppError(503,'sat_disabled','La consulta de vigencia al SAT está desactivada en este servidor.')
        verdict=await self.sat.verify(ticket['validation'])
        ticket['validation']['sat_verified']=bool(verdict.get('query_success') and verdict.get('vigente'))
        ticket['validation']['sat_checked_at']=time.time()
        if not ticket['validation']['sat_verified']:
            return public(self._status(owner,ticket,Status.REQUIERE_INTERVENCION,'El SAT no confirmó que el CFDI esté vigente.'))
        ticket=self._status(owner,ticket,Status.FACTURA_VALIDADA,'XML cotejado y vigencia consultada al SAT; no es una verificación criptográfica de sellos.')
        return self._finish_verified_invoice(owner,ticket)

    def delete_profile(self,owner,profile_id):
        profile=self.store.get('profile',owner,profile_id)
        related={ticket['id'] for ticket in self.store.list('ticket',owner) if ticket['profile_id']==profile_id}
        if any(record['ticket_id'] in related and record['_state']=='claimed'
                for kind in ('email_approval','portal_send_review') for record in self.store.list(kind,owner)):
            raise AppError(409,'send_in_progress','Espera a que termine el envío antes de eliminar el perfil.')
        for ticket in self.store.list('ticket',owner):
            if ticket['profile_id']==profile_id:
                self.delete_ticket(owner,ticket['id'])
        for draft in self.store.list('profile_draft',owner):
            if draft.get('replace_profile_id')==profile_id:
                self.store.delete('profile_draft',owner,draft['id'])
        try:
            self.store.delete('document',owner,profile['document_id'])
        except AppError as error:
            if error.status!=404:
                raise
        self.store.delete('profile',owner,profile_id)
        self.store.audit(owner,profile_id,'profile_deleted')

    def delete_ticket(self,owner,ticket_id):
        ticket=self.store.get('ticket',owner,ticket_id)
        if any(record['ticket_id']==ticket_id and record['_state']=='claimed'
                for record in self.store.list('portal_send_review',owner)):
            raise AppError(409,'send_in_progress','Espera a que termine el envío antes de eliminar el ticket.')
        for kind in ('email_approval','mail_reply','invoice_uuid','portal_permission','portal_grant','browser_review','portal_data','portal_data_review','portal_fill_review','portal_transaction','portal_send_review','portal_field_photo'):
            for record in self.store.list(kind,owner):
                if record['ticket_id']==ticket_id:
                    if kind=='email_approval' and record['_state']=='claimed':
                        raise AppError(409,'send_in_progress','Espera a que termine el envío antes de eliminar el ticket.')
                    self.store.delete(kind,owner,record['id'])
        aliases=[item['id'] for item in self.store.list('document',owner) if item.get('duplicate_ticket_id')==ticket_id or item.get('ticket_id')==ticket_id]
        for identifier in dict.fromkeys([ticket['document_id'],*ticket['invoice_documents'],*aliases]):
            try:
                self.store.delete('document',owner,identifier)
            except AppError as error:
                if error.status!=404:
                    raise
        self.store.delete('ticket',owner,ticket_id)
