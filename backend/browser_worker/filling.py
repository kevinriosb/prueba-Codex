"""Llenado de controles observados. La red queda cerrada antes de introducir valores."""
import asyncio
import re
import time

from .policy import BrowserError


FIELD_SIGNATURE="e=>JSON.stringify([e.tagName,e.type,e.name,e.id,e.required,e.readOnly,e.disabled,e.maxLength,e.form?.getAttribute('action'),e.form?.getAttribute('method')])"
TEXT_TYPES={'text','email','tel','url','search','number','date','time','datetime-local','month','week'}


async def fill_fields(manager,owner,identifier,observation_id,values):
    session=await manager.get(owner,identifier)
    async with session.lock:
        if session.private:
            raise BrowserError('browser_private','La sesión ya contiene un llenado; no se repetirá.')
        if observation_id!=session.observation:
            raise BrowserError('browser_stale','El formulario cambió. Revisa los datos antes de llenar.')
        if session.network_blocked or session.captcha or session.login_required:
            raise BrowserError('browser_intervention','El formulario requiere intervención humana.')
        if not values or len(values)>24 or len({item['reference'] for item in values})!=len(values):
            raise BrowserError('browser_fill','Campos de llenado no válidos.',422)
        prepared=[]
        for item in values:
            reference,value=item['reference'],item['value']
            if not re.fullmatch('[a-f0-9]{8}',reference) or reference not in session.field_controls:
                raise BrowserError('browser_stale','Un campo ya no corresponde al formulario observado.')
            if not isinstance(value,str) or not value or len(value)>250 or any(ord(char)<32 for char in value):
                raise BrowserError('browser_fill','El valor de un campo no es válido.',422)
            frame,field=session.field_controls[reference]
            if frame.url!=field['frame_url']:
                raise BrowserError('browser_stale','El destino del formulario cambió. Revisa la página actual.')
            locator=frame.locator('[data-public-field-ref="'+field['selector']+'"]')
            if await locator.count()!=1 or not await locator.is_visible() or await locator.evaluate(FIELD_SIGNATURE)!=field['signature']:
                raise BrowserError('browser_stale','Un control cambió. Revisa el formulario.')
            if field['kind']=='input' and field['input_type'] not in TEXT_TYPES:
                raise BrowserError('browser_fill','Ese tipo de campo requiere captura manual.',422)
            if field['max_length'] is not None and len(value)>field['max_length']:
                raise BrowserError('browser_fill','Un valor supera el tamaño admitido por el campo.',422)
            option=None
            if field['kind']=='select':
                options=await locator.evaluate("e=>Array.from(e.options).filter(o=>!o.disabled).map(o=>({value:o.value,label:o.text.trim()}))")
                matches=[o for o in options if o['value']==value or o['label'].casefold()==value.casefold()]
                if not matches and re.fullmatch('[A-Z0-9]{2,4}',value):
                    matches=[o for o in options if re.match(re.escape(value)+r'(?:\s|[-·:])',o['label'],re.I)]
                if len(matches)!=1:
                    raise BrowserError('browser_fill','La opción del campo no coincide de forma única; requiere revisión.',422)
                option=matches[0]['value']
            if not field['editable'] and await locator.input_value()!=value:
                raise BrowserError('browser_fill','Un campo de solo lectura no coincide con los datos revisados.',422)
            prepared.append((reference,locator,field,value,option))
        # Barrera antes del primer valor: ningún evento input/change puede
        # enviar datos por fetch, navegación, imágenes, formularios o sockets.
        session.private=True
        session.created=time.monotonic()
        session.actions.clear()
        session.cache.clear()
        session.private_blocked=0
        try:
            async with asyncio.timeout(10):
                await session.context.set_offline(True)
                frames={id(frame):frame for frame,_ in session.field_controls.values()}
                for frame in frames.values():
                    await frame.evaluate("""() => {
                        document.addEventListener('submit',e=>{e.preventDefault();e.stopImmediatePropagation();},true);
                        HTMLFormElement.prototype.submit=function(){};
                        HTMLFormElement.prototype.requestSubmit=function(){};
                    }""")
                for reference,locator,field,value,option in prepared:
                    if field['editable']:
                        if option is not None:
                            await locator.select_option(value=option,timeout=1200)
                        else:
                            await locator.fill(value,timeout=1200)
                    expected=option if option is not None else value
                    if await locator.input_value()!=expected:
                        raise BrowserError('browser_fill','Un campo no conservó el valor revisado.')
        except Exception:
            manager.sessions.pop(identifier,None)
            await manager._dispose(session)
            raise BrowserError('browser_fill','No se completó el llenado. La red estuvo cerrada; no se envió el formulario.') from None
        result={'session_id':identifier,'observation_id':observation_id,'filled':True,
            'filled_references':[item[0] for item in prepared],'network_frozen':True,
            'submitted':False,'purchase_validated':False,'expires_in':manager.ttl,
            'blocked_requests':session.private_blocked}
        session.fill_result=result
        return result


async def fill_status(manager,owner,identifier):
    session=await manager.get(owner,identifier)
    async with session.lock:
        if not session.private or not session.fill_result:
            raise BrowserError('browser_fill','No hay un formulario preparado en esta sesión.',404)
        return session.fill_result|{'expires_in':max(0,int(manager.ttl-(time.monotonic()-session.created))),
            'blocked_requests':session.private_blocked}
