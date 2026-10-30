import base64
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import Field

from ..auth import authenticated, check_csrf
from ..errors import AppError
from .service import public


def create_billing_router(payload_base):
    router=APIRouter(prefix='/api/billing',tags=['Facturación privada'])

    class Analyze(payload_base):
        consent_to_ai: bool = Field(strict=True)
        replace_profile_id: uuid.UUID | None = None

    class ConfirmProfile(payload_base):
        cfdi_use: str = Field(max_length=4)
        email: str = Field(max_length=254)
        confirmed: bool = Field(strict=True)

    class Confirm(payload_base):
        confirmed: bool = Field(strict=True)

    class Prepare(payload_base):
        include_csf: bool = Field(default=False,strict=True)
        authorize_csf: bool = Field(default=False,strict=True)

    class Track(payload_base):
        authorize_sat: bool = Field(default=False,strict=True)

    class Tracking(Confirm):
        enabled: bool = Field(strict=True)

    class BrowserAction(payload_base):
        observation_id:str=Field(pattern=r'^[a-f0-9]{8}$')
        reference:str=Field(pattern=r'^[a-f0-9]{8}$')

    class PortalPermissionRequest(payload_base):
        origins:list[str]|None=Field(default=None,max_length=12)
        intent:Literal['browse','details']='browse'

    class PortalPermissionDecision(payload_base):
        approve:bool=Field(strict=True)

    class PortalBookmark(Confirm):
        observation_id:str=Field(pattern=r'^[a-f0-9]{8}$')

    class PortalSelection(Confirm):
        use_saved:bool=Field(strict=True)

    class CertificateRequired(Confirm):
        evidence: str = Field(min_length=10,max_length=1000)

    def service(request):
        billing=request.app.state.billing
        billing.require_enabled()
        return billing

    @router.get('/config')
    async def configuration(request:Request,user=Depends(authenticated)):
        return request.app.state.billing.overview(user['id'])

    @router.post('/documents',dependencies=[Depends(check_csrf)])
    async def upload(request:Request,purpose:Literal['csf','ticket'],profile_id:uuid.UUID|None=None,user=Depends(authenticated)):
        return service(request).receive_document(user['id'],purpose,await request.body(),profile_id=str(profile_id) if profile_id else None)

    @router.get('/documents/{identifier}')
    async def download(identifier:uuid.UUID,request:Request,user=Depends(authenticated)):
        record=service(request).document(user['id'],str(identifier))
        extension={'application/pdf':'pdf','application/xml':'xml','image/jpeg':'jpg'}[record['mime']]
        return Response(base64.b64decode(record['data']),media_type=record['mime'],headers={
            'Content-Disposition':f'attachment; filename="{record["purpose"]}.{extension}"'})

    @router.delete('/documents/{identifier}',dependencies=[Depends(check_csrf)])
    async def delete_document(identifier:uuid.UUID,payload:Confirm,request:Request,user=Depends(authenticated)):
        if payload.confirmed is not True:
            raise AppError(409,'delete_confirmation','Confirma la eliminación del archivo.')
        service(request).store.delete('document',user['id'],str(identifier))
        return {'ok':True}

    @router.post('/documents/{identifier}/analyze',dependencies=[Depends(check_csrf)])
    async def analyze(identifier:uuid.UUID,payload:Analyze,request:Request,user=Depends(authenticated)):
        billing=service(request)
        document=billing.document(user['id'],str(identifier))
        if document['purpose']=='csf':
            return {'type':'fiscal_draft','draft':await billing.process_fiscal_certificate(user['id'],str(identifier),
                consent=payload.consent_to_ai,replace_profile_id=str(payload.replace_profile_id) if payload.replace_profile_id else None)}
        return {'type':'ticket','ticket':await billing.analyze_ticket(user['id'],str(identifier),consent=payload.consent_to_ai)}

    @router.get('/profiles/drafts/{identifier}')
    async def draft(identifier:uuid.UUID,request:Request,user=Depends(authenticated)):
        return service(request).extract_fiscal_profile(user['id'],str(identifier))

    @router.post('/profiles/drafts/{identifier}/confirm',dependencies=[Depends(check_csrf)])
    async def confirm_profile(identifier:uuid.UUID,payload:ConfirmProfile,request:Request,user=Depends(authenticated)):
        return service(request).confirm_fiscal_profile(user['id'],str(identifier),**payload.model_dump())

    @router.delete('/profiles/{identifier}',dependencies=[Depends(check_csrf)])
    async def delete_profile(identifier:uuid.UUID,payload:Confirm,request:Request,user=Depends(authenticated)):
        if payload.confirmed is not True:
            raise AppError(409,'delete_confirmation','Confirma la eliminación del perfil y sus solicitudes y archivos asociados.')
        service(request).delete_profile(user['id'],str(identifier))
        return {'ok':True}

    @router.get('/tickets/{identifier}')
    async def ticket(identifier:uuid.UUID,request:Request,user=Depends(authenticated)):
        billing=service(request)
        return public(billing.store.get('ticket',user['id'],str(identifier)))|{'channels':billing.detect_billing_channels(user['id'],str(identifier)),
            'responses':[public(value) for value in billing.store.list('mail_reply',user['id']) if value['ticket_id']==str(identifier)]}

    @router.post('/tickets/{identifier}/browser',dependencies=[Depends(check_csrf)])
    async def open_browser(identifier:uuid.UUID,request:Request,user=Depends(authenticated)):
        return public(await service(request).browse_portal(user['id'],str(identifier)))

    @router.post('/tickets/{identifier}/portal-permissions',dependencies=[Depends(check_csrf)])
    async def request_permission(identifier:uuid.UUID,payload:PortalPermissionRequest,request:Request,user=Depends(authenticated)):
        record=service(request).portal_permissions.request(user['id'],str(identifier),**payload.model_dump())
        return public(record)|{'state':record['_state'],'expires':record['_expires']}

    @router.post('/portal-permissions/{identifier}/decision',dependencies=[Depends(check_csrf)])
    async def decide_permission(identifier:uuid.UUID,payload:PortalPermissionDecision,request:Request,user=Depends(authenticated)):
        billing=service(request)
        record=billing.portal_permissions.decide(user['id'],str(identifier),approve=payload.approve)
        await billing.sync_portal_permissions(user['id'],record['ticket_id'],cancel=not payload.approve)
        return {'approved':payload.approve,'ticket_id':record['ticket_id'],'purpose':'public_read'}

    @router.post('/tickets/{identifier}/browser/actions',dependencies=[Depends(check_csrf)])
    async def browser_action(identifier:uuid.UUID,payload:BrowserAction,request:Request,user=Depends(authenticated)):
        return public(await service(request).browse_portal(user['id'],str(identifier),**payload.model_dump()))

    @router.get('/portals')
    async def remembered_portals(request:Request,user=Depends(authenticated)):
        return [{'id':item['id'],'merchant':item['merchant'],'url':item['url'],'expires':item['_expires']}
            for item in service(request).store.list('portal_memory',user['id'])]

    @router.post('/tickets/{identifier}/portal-bookmark',dependencies=[Depends(check_csrf)])
    async def remember_portal(identifier:uuid.UUID,payload:PortalBookmark,request:Request,user=Depends(authenticated)):
        if not payload.confirmed:
            raise AppError(409,'portal_memory_confirmation','Confirma que es el portal correcto de ese comercio.')
        record=service(request).portal_memory.save(user['id'],str(identifier),payload.observation_id)
        return {'id':record['id'],'merchant':record['merchant'],'url':record['url'],'expires':record['_expires']}

    @router.post('/tickets/{identifier}/portal-selection',dependencies=[Depends(check_csrf)])
    async def select_portal(identifier:uuid.UUID,payload:PortalSelection,request:Request,user=Depends(authenticated)):
        if not payload.confirmed:
            raise AppError(409,'portal_memory_confirmation','Confirma el enlace que deseas utilizar.')
        return public(await service(request).portal_memory.select_checked(user['id'],str(identifier),remembered=payload.use_saved))

    @router.delete('/portals/{identifier}',dependencies=[Depends(check_csrf)])
    async def forget_portal(identifier:uuid.UUID,payload:Confirm,request:Request,user=Depends(authenticated)):
        if not payload.confirmed:
            raise AppError(409,'portal_memory_confirmation','Confirma que deseas olvidar el enlace.')
        service(request).portal_memory.forget(user['id'],str(identifier))
        return {'ok':True}

    @router.delete('/tickets/{identifier}',dependencies=[Depends(check_csrf)])
    async def delete_ticket(identifier:uuid.UUID,payload:Confirm,request:Request,user=Depends(authenticated)):
        if payload.confirmed is not True:
            raise AppError(409,'delete_confirmation','Confirma la eliminación de la solicitud y sus archivos.')
        service(request).delete_ticket(user['id'],str(identifier))
        return {'ok':True}

    @router.post('/tickets/{identifier}/verify-email',dependencies=[Depends(check_csrf)])
    async def verify_email(identifier:uuid.UUID,payload:Confirm,request:Request,user=Depends(authenticated)):
        return service(request).verify_billing_email(user['id'],str(identifier),confirmed=payload.confirmed)

    @router.post('/tickets/{identifier}/inspect-portal',dependencies=[Depends(check_csrf)])
    async def inspect_portal(identifier:uuid.UUID,request:Request,user=Depends(authenticated)):
        return public(await service(request).inspect_billing_portal(user['id'],str(identifier)))

    @router.post('/tickets/{identifier}/prepare-email',dependencies=[Depends(check_csrf)])
    async def prepare_email(identifier:uuid.UUID,payload:Prepare,request:Request,user=Depends(authenticated)):
        return service(request).request_email_send_approval(user['id'],str(identifier),**payload.model_dump())

    @router.get('/email-approvals/{identifier}')
    async def view_approval(identifier:uuid.UUID,request:Request,user=Depends(authenticated)):
        record=service(request).store.get('email_approval',user['id'],str(identifier))
        return public(record)|{'expires':record['_expires'],'state':record['_state']}

    @router.post('/email-approvals/{identifier}/send',dependencies=[Depends(check_csrf)])
    async def send_email(identifier:uuid.UUID,payload:Confirm,request:Request,user=Depends(authenticated)):
        return await service(request).send_invoice_request_email(user['id'],str(identifier),confirmed=payload.confirmed)

    @router.post('/email-approvals/{identifier}/reject',dependencies=[Depends(check_csrf)])
    async def reject_email(identifier:uuid.UUID,request:Request,user=Depends(authenticated)):
        return service(request).reject_email(user['id'],str(identifier))

    @router.post('/tickets/{identifier}/certificate-required',dependencies=[Depends(check_csrf)])
    async def certificate_required(identifier:uuid.UUID,payload:CertificateRequired,request:Request,user=Depends(authenticated)):
        return service(request).mark_certificate_required(user['id'],str(identifier),**payload.model_dump())

    @router.post('/tickets/{identifier}/track',dependencies=[Depends(check_csrf)])
    async def track(identifier:uuid.UUID,payload:Track,request:Request,user=Depends(authenticated)):
        return await service(request).track_invoice_email(user['id'],str(identifier),authorize_sat=payload.authorize_sat)

    @router.post('/tickets/{identifier}/validate',dependencies=[Depends(check_csrf)])
    async def validate(identifier:uuid.UUID,payload:Track,request:Request,user=Depends(authenticated)):
        return await service(request).validate_cfdi(user['id'],str(identifier),authorize_sat=payload.authorize_sat)

    @router.post('/tickets/{identifier}/tracking',dependencies=[Depends(check_csrf)])
    async def tracking(identifier:uuid.UUID,payload:Tracking,request:Request,user=Depends(authenticated)):
        return service(request).configure_tracking(user['id'],str(identifier),**payload.model_dump())

    @router.post('/gmail/connect',dependencies=[Depends(check_csrf)])
    async def connect_gmail(request:Request,user=Depends(authenticated)):
        return {'authorization_url':service(request).mail.connect(user['id'])}

    @router.get('/gmail/callback')
    async def gmail_callback(request:Request,state:str='',code:str=''):
        if not state or not code or len(state)>200 or len(code)>2000:
            raise AppError(400,'gmail_callback','La conexión fue cancelada o no es válida.')
        await service(request).mail.finish_connect(state,code)
        return RedirectResponse('/?gmail=connected',status_code=303)

    @router.post('/gmail/disconnect',dependencies=[Depends(check_csrf)])
    async def disconnect_gmail(payload:Confirm,request:Request,user=Depends(authenticated)):
        if payload.confirmed is not True:
            raise AppError(409,'disconnect_confirmation','Confirma la desconexión de Gmail.')
        billing=service(request)
        account=billing.mail.account(user['id'])
        if account:
            billing.store.delete('gmail',user['id'],account['id'])
        return {'ok':True}

    return router
