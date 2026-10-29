"""Pasos privados de facturación, separados de la navegación pública y del llenado aislado.

Cada clic usa una referencia DOM y una autorización de un solo uso. Una respuesta
incierta nunca provoca otro clic. No ejecuta código proporcionado por el modelo.
"""
import asyncio
import base64
import hashlib
import re
import secrets
import time
from contextlib import suppress
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError

from .filling import FIELD_SIGNATURE,TEXT_TYPES
from .policy import BrowserError,https_url,origin,tracking_url,RESPONSE_BYTE_LIMIT


ACTION_SIGNATURE="e=>JSON.stringify([e.tagName,e.type,e.name,e.id,e.getAttribute('href'),e.getAttribute('onclick'),e.form?.action,e.form?.method,e.textContent,e.value])"
CONTROLS=r'''() => {
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const nodes=Array.from(document.querySelectorAll('button,input[type=submit],input[type=button],input[type=image],a[href],a[onclick]'));
 return nodes.filter(e=>visible(e)&&!e.disabled).slice(0,100).map(e=>{
  const label=(e.getAttribute('aria-label')||e.getAttribute('title')||e.innerText||e.value||e.alt||'').replace(/\s+/g,' ').trim().slice(0,100);
  const selector='bill-'+Math.random().toString(36).slice(2,12);e.setAttribute('data-billing-action',selector);
  return {selector,label,href:e.tagName==='A'?e.href:null,target:e.form?.action||document.URL,
   signature:JSON.stringify([e.tagName,e.type,e.name,e.id,e.getAttribute('href'),e.getAttribute('onclick'),e.form?.action,e.form?.method,e.textContent,e.value])};
 });
}'''
FEEDBACK=r'''() => {
 const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
 const alerts=Array.from(document.querySelectorAll('[role=alert],.validation-summary-errors,.field-validation-error,.alert-danger,[aria-invalid=true]')).filter(visible).map(e=>e.innerText||e.validationMessage||'Dato inválido');
 const text=(document.body?.innerText||'').slice(0,30000);
 return {error:alerts.join(' ').replace(/\s+/g,' ').trim().slice(0,350),
  success:/(?:factura\s+(?:ha sido\s+|fue\s+)?(?:generada|emitida|enviada)|solicitud\s+(?:ha sido\s+|fue\s+)?(?:recibida|registrada)|invoice\s+(?:issued|generated|sent))/i.test(text)};
}'''


def action_kind(label,href):
    if re.search(r'\b(xml|pdf)\b',label,re.I) or href and re.search(r'\.(?:xml|pdf)(?:[?#]|$)|[?&](?:format|formato|tipo|type)=(?:xml|pdf)(?:[&#]|$)',href,re.I):
        return 'download'
    if re.search(r'cancel|eliminar|borrar|regresar|volver|atr[aá]s|salir|ayuda|privacidad|t[eé]rminos',label,re.I):return None
    if re.search(r'(?:generar|emitir|solicitar|crear|obtener|enviar|confirmar|facturar|timbrar|issue|generate|request|submit|send)',label,re.I):return 'issue'
    if re.search(r'continuar|siguiente|validar|buscar|consultar|aceptar|registrar|agregar|next|continue|search|validate',label,re.I):return 'continue'
    return None


async def transaction_view(manager,identifier,session):
    if session.private and not session.transaction.get('started'):
        raise BrowserError('browser_private','El llenado aislado debe revisarse en una sesión nueva.')
    snapshot=await manager._observe(identifier,session,transaction=True)
    if session.transaction.get('auxiliary_blocked'):snapshot['partial']=True
    actions=[];controls={};feedback={'error':'','success':False}
    for frame in session.page.frames[:8]:
        try:
            if origin(frame.url) not in session.allowed:continue
            if frame!=session.page.main_frame and not await (await frame.frame_element()).is_visible():continue
            for item in await frame.evaluate(CONTROLS):
                kind=action_kind(item['label'],item['href'])
                if not kind:continue
                if kind=='download':
                    try:
                        target=https_url(item['href']) if item['href'] and not item['href'].startswith('javascript:') else None
                    except BrowserError:continue
                    if not target or origin(target) not in session.transaction.get('origins',session.allowed):continue
                    item['href']=target
                else:
                    try:target=https_url(item['target'])
                    except BrowserError:continue
                ref=secrets.token_hex(4)
                controls[ref]=(frame,item|{'frame_url':frame.url,'kind':kind})
                actions.append({'reference':ref,'label':item['label'],'kind':kind,'origin':origin(target)})
            info=await frame.evaluate(FEEDBACK)
            feedback['error']=feedback['error'] or info['error']
            feedback['success']=feedback['success'] or info['success']
        except PlaywrightError:
            snapshot['partial']=True
    session.transaction['controls']=controls
    session.transaction['feedback']=feedback
    session.transaction['view']={'snapshot':snapshot,'actions':actions[:12],
        'error':feedback['error'],'success':feedback['success'],
        'files':[{'reference':ref,'mime':item['mime'],'sha256':item['sha256']} for ref,item in session.transaction.get('files',{}).items()]}
    return session.transaction['view']


async def transaction_route(manager,session,route):
    """El mismo transporte IP/TLS del lector, con POST únicamente al portal confirmado."""
    request=route.request
    tx=session.transaction
    try:
        url=https_url(request.url)
        target=origin(url)
        if tracking_url(url) or request.resource_type in {'image','font','media','other'}:
            await route.abort('blockedbyclient');return
        method=request.method
        resource=request.resource_type in {'script','stylesheet'} and method=='GET'
        if resource and target not in tx['origins'] and url not in session.public_resources:
            tx['auxiliary_blocked']=True
            await route.abort('blockedbyclient');return
        if not tx.get('network') or not resource and target not in tx['origins']:
            raise BrowserError('browser_submission_origin','El siguiente destino necesita revisión.')
        if method not in {'GET','POST'} or tx.get('downloads_only') and method!='GET':
            raise BrowserError('browser_submission_method','Método del portal no admitido.')
        manager._count_request(session)
        headers=await request.all_headers()
        if resource and target not in tx['origins']:
            headers={key:value for key,value in headers.items() if key not in {'cookie','referer','authorization',
                'x-csrf-token','x-xsrf-token','requestverificationtoken','x-requested-with'}}
        body=request.post_data_buffer if method=='POST' else None
        if not tx.get('network'):raise BrowserError('browser_submission','El paso terminó.')
        # Marcar ANTES de conectar: una desconexión no demuestra que no se envió.
        if not resource:tx['dispatched']=True
        status,response_headers,content=await manager.fetcher.request_limited(method,url,headers,body,
            on_bytes=lambda size:charge_transaction(session,size))
        if status>=400:tx['transport_error']=True
        location=response_headers.get('location')
        if location:
            destination=https_url(urljoin(url,location))
            if origin(destination) not in tx['origins'] and not (resource and destination in session.public_resources):
                raise BrowserError('browser_submission_origin','Redirección a otro proveedor; requiere revisión.')
            response_headers=response_headers|{'location':destination}
        mime=response_headers.get('content-type','').split(';')[0].lower()
        if mime=='application/pdf' or content.startswith(b'%PDF-') or b'http://www.sat.gob.mx/cfd/' in content[:4000]:
            mime='application/pdf' if content.startswith(b'%PDF-') else 'application/xml'
            files=tx.setdefault('files',{})
            sha=hashlib.sha256(content).hexdigest()
            if not any(item['sha256']==sha for item in files.values()):
                if len(files)>=6:raise BrowserError('browser_submission','Demasiados adjuntos.')
                files[secrets.token_hex(4)]={'mime':mime,'data':base64.b64encode(content).decode(),'sha256':sha}
            # El archivo queda en memoria privada del worker. No abrir un visor externo.
            await route.fulfill(status=200,content_type='text/html',body='<p>Archivo de factura recibido.</p>')
            return
        await route.fulfill(status=status,headers=response_headers,body=content)
    except Exception:
        tx['transport_error']=True
        with suppress(PlaywrightError):await route.abort('blockedbyclient')


def charge_transaction(session,size):
    session.size+=size
    if session.size>25_165_824 or size>RESPONSE_BYTE_LIMIT:
        raise BrowserError('browser_submission','El portal superó el tamaño admitido.')


async def transaction_send(manager,owner,identifier,*,operation_id,observation_id,reference,values,origins):
    session=await manager.get(owner,identifier)
    async with session.lock:
        tx=session.transaction
        previous=tx.setdefault('operations',{}).get(operation_id)
        if previous:return previous
        if observation_id!=session.observation or reference not in tx.get('controls',{}):
            raise BrowserError('browser_stale','El paso cambió. Revisa el formulario.')
        if tx.get('uncertain') or tx.get('issued'):
            raise BrowserError('browser_submission','Solicitud enviada o sin confirmar. No se repetirá.')
        if session.network_blocked or session.captcha or session.login_required or (tx.get('view') or {}).get('snapshot',{}).get('partial'):
            raise BrowserError('browser_intervention','El portal requiere intervención humana.')
        frame,action=tx['controls'][reference]
        if action['kind']=='download':raise BrowserError('browser_submission','Acción de envío no válida.',422)
        permitted={origin(value) for value in origins}
        required={origin(frame.url),origin(action['target']),origin(session.page.url)}
        if not required<=permitted or len(permitted)>4:
            raise BrowserError('browser_submission','Revisa los destinos de este formulario.',422)
        if len(tx['operations'])>=8:raise BrowserError('browser_submission','Se alcanzó el máximo de pasos; revisa la solicitud.')
        locator=frame.locator('[data-billing-action="'+action['selector']+'"]')
        if frame.url!=action['frame_url'] or await locator.count()!=1 or await locator.evaluate(ACTION_SIGNATURE)!=action['signature']:
            raise BrowserError('browser_stale','El botón del portal cambió.')
        prepared=[]
        if len(values)>24 or len({v['reference'] for v in values})!=len(values):
            raise BrowserError('browser_submission','Campos no válidos.',422)
        for item in values:
            field_frame,field=session.field_controls.get(item['reference'],(None,None))
            value=item['value']
            if not field or not value or len(value)>250 or any(ord(c)<32 for c in value):
                raise BrowserError('browser_stale','El campo o valor cambió.')
            control=field_frame.locator('[data-public-field-ref="'+field['selector']+'"]')
            if (field_frame.url!=field['frame_url'] or await control.count()!=1 or not await control.is_visible()
                    or await control.evaluate(FIELD_SIGNATURE)!=field['signature']):
                raise BrowserError('browser_stale','El campo ya no corresponde al dato aprobado.')
            option=None
            if field['max_length'] is not None and len(value)>field['max_length']:
                raise BrowserError('browser_submission','El dato excede el tamaño del campo.',422)
            if field['kind']=='select':
                options=await control.evaluate("e=>Array.from(e.options).filter(o=>!o.disabled).map(o=>({value:o.value,label:o.text.trim()}))")
                matches=[o for o in options if o['value']==value or o['label'].casefold()==value.casefold() or re.fullmatch('[A-Z0-9]{2,4}',value) and re.match(re.escape(value)+r'(?:\s|[-·:])',o['label'],re.I)]
                if len(matches)!=1:raise BrowserError('browser_submission','Elige una opción válida para el campo.',422)
                option=matches[0]['value']
            elif field['kind']=='input' and field['input_type'] not in TEXT_TYPES:
                raise BrowserError('browser_intervention','Ese campo requiere captura manual.')
            if not field['editable'] and await control.input_value()!=(option or value):
                raise BrowserError('browser_submission','Un dato fijo del portal no coincide.',422)
            prepared.append((control,field,value,option))
        # Reserva antes de introducir datos o clicar. Incluso una caída conserva la incertidumbre.
        tx['operations'][operation_id]={'operation_id':operation_id,'state':'unknown','view':tx['view']}
        tx.update(started=True,network=False,dispatched=False,transport_error=False,auxiliary_blocked=False,origins=permitted)
        session.private=True
        session.created=time.monotonic()
        session.cache.clear()
        try:
            async with asyncio.timeout(18):
                await session.context.set_offline(True)
                for control,field,value,option in prepared:
                    if field['editable']:
                        if option is not None:await control.select_option(value=option,timeout=1200)
                        else:await control.fill(value,timeout=1200)
                    if await control.input_value()!=(option if option is not None else value):
                        raise BrowserError('browser_submission','El control rechazó el dato.',422)
                invalid=await frame.evaluate("() => Array.from(document.querySelectorAll('input,select,textarea')).some(e=>e.getClientRects().length&&!e.disabled&&!e.checkValidity())")
                if invalid:
                    tx['operations'][operation_id]={'operation_id':operation_id,'state':'correction','view':await transaction_view(manager,identifier,session)}
                    return tx['operations'][operation_id]
                if (frame.url!=action['frame_url'] or await locator.count()!=1
                        or await locator.evaluate(ACTION_SIGNATURE)!=action['signature']):
                    raise BrowserError('browser_stale','El botón cambió al introducir los datos.')
                tx['network']=True
                await session.context.set_offline(False)
                await locator.click(timeout=6000)
                with suppress(PlaywrightError):await session.page.wait_for_load_state('networkidle',timeout=2500)
                view=await transaction_view(manager,identifier,session)
                # Descargar únicamente enlaces XML/PDF observados del mismo proveedor.
                for ref,(download_frame,item) in list(tx['controls'].items()):
                    if item['kind']!='download':continue
                    tx['downloads_only']=True
                    await download_frame.evaluate("url=>fetch(url,{credentials:'same-origin'}).then(r=>r.arrayBuffer()).then(()=>true)",item['href'])
                view['files']=[{'reference':ref,'mime':item['mime'],'sha256':item['sha256']} for ref,item in tx.get('files',{}).items()]
                old_fields=[(f['label'],f['kind'],f['required']) for f in tx['operations'][operation_id]['view']['snapshot']['fields']]
                new_fields=[(f['label'],f['kind'],f['required']) for f in view['snapshot']['fields']]
                if view['files']:state='submitted'
                elif view['error']:state='correction'
                elif tx['transport_error'] and not view['files']:state='unknown'
                elif view['success'] and tx['dispatched']:state='submitted'
                elif new_fields and old_fields!=new_fields:state='review'
                elif action['kind']=='issue':state='unknown'
                else:state='review'
                tx['issued']=state=='submitted'
                tx['uncertain']=state=='unknown'
                tx['operations'][operation_id]={'operation_id':operation_id,'state':state,'view':view}
        except Exception:
            tx['uncertain']=bool(tx.get('dispatched'))
            if not tx['uncertain']:
                try:
                    view=await transaction_view(manager,identifier,session)
                    view['error']=view['error'] or 'Un campo no admitió el dato. Corrígelo antes de enviar.'
                    tx['operations'][operation_id]={'operation_id':operation_id,'state':'correction','view':view}
                except Exception:
                    tx['uncertain']=True
        finally:
            tx['network']=False
            tx['downloads_only']=False
            with suppress(PlaywrightError):await session.context.set_offline(True)
        return tx['operations'][operation_id]


async def transaction_status(manager,owner,identifier,operation_id):
    session=await manager.get(owner,identifier)
    async with session.lock:
        tx=session.transaction
        result=tx.get('operations',{}).get(operation_id)
        if not result:raise BrowserError('browser_session','Paso no disponible.',404)
        # Recuperar evidencia ya recibida tras una respuesta interrumpida. Cero red y cero clics.
        if result['state']=='unknown' and not tx.get('network') and (tx.get('files') or tx.get('feedback',{}).get('success') and tx.get('dispatched')):
            view=tx['view']|{'files':[{'reference':ref,'mime':item['mime'],'sha256':item['sha256']} for ref,item in tx.get('files',{}).items()]}
            result={'operation_id':operation_id,'state':'submitted','view':view}
            tx['operations'][operation_id]=result
            tx.update(uncertain=False,issued=True)
        return result
