"""Diagnóstico público por stdin en browser: sin IA, perfiles, tickets ni envíos."""
import argparse
import asyncio
import json

from backend.browser_worker.engine import BrowserManager
from backend.browser_worker.policy import BrowserError,secure_link,origin,public_page,bookmark_url,RESOURCE_POLICY_VERSION
from backend.app.billing.portal_inspection import field_key
from backend.app.billing.portal_navigation import next_public_action
from backend.app.billing.portal_memory import has_billing_form


LAYOUT=r'''() => Array.from(document.querySelectorAll('a:has(img),img[onclick],input[type=image]')).slice(0,50).map(e=>{
    const img=e.tagName==='IMG'||e.tagName==='INPUT'?e:e.querySelector('img');
    let image='';try{if(img?.getAttribute('src'))image=new URL(img.getAttribute('src'),document.baseURI).pathname.split('/').pop().slice(0,100);}catch{}
    const hidden=[];for(let p=e,i=0;p&&i<30;p=p.parentElement,i++){
        const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||p.hidden)hidden.push({tag:p.tagName,display:s.display,visibility:s.visibility});
    }
    return {tag:e.tagName,form:!!(e.closest('form')||e.form),label:(e.getAttribute('aria-label')||img?.alt||e.title||'').slice(0,100),
        image,rects:e.getClientRects().length,display:getComputedStyle(e).display,visibility:getComputedStyle(e).visibility,
        hiddenAncestors:hidden.slice(0,4),hasLink:!!e.getAttribute('href'),hasHandler:!!e.getAttribute('onclick'),
        imageComplete:!!img?.complete,imageWidth:img?.naturalWidth||0};
})'''


async def diagnose(url,merchant='',steps=8,*,manager=None,emit=print,layout=False):
    manager=manager or BrowserManager()
    user='diagnostic-public-only'
    try:
        url=secure_link(url)
        url=bookmark_url(url) or public_page(url)
        emit('Diagnóstico público sin IA. Política de recursos: '+str(RESOURCE_POLICY_VERSION))
        current=await manager.open(user,url,[origin(url)],public_route=True)
        for step in range(steps):
            for item in current['fields']:
                item['key']=field_key(item['label'])
            report={key:current.get(key) for key in ('page','block_reason','captcha','login_required','brand_selection','partial','omitted_writes','diagnostics')}
            report['step']=step+1
            report['fields']=[{'label':item['label'],'key':item['key']} for item in current['fields']]
            report['actions']=[{'label':item['label'],'kind':item['kind'],'brand':item.get('brand',False)} for item in current['actions']]
            if layout:
                session=await manager.get(user,current['session_id'])
                report['layout']=await session.page.evaluate(LAYOUT)
            emit(json.dumps(report,ensure_ascii=False))
            if current['network_blocked'] or current['captcha'] or current.get('login_required'):
                emit('Resultado: detenido; consulta la causa y los recursos del diagnóstico.')
                return 2
            if has_billing_form(current):
                emit('Resultado: formulario observado. No se solicitó una factura ni se enviaron datos fiscales.')
                return 0
            action=next_public_action({'merchant':merchant},current)
            if not action or step==steps-1:
                emit('Resultado: faltan campos o una elección pública inequívoca; no se inventó una ruta.')
                return 2
            current=await manager.act(user,current['session_id'],current['observation_id'],action['reference'])
    except BrowserError as error:
        emit('Resultado: '+error.code+'. No se mostrarán cabeceras, cookies ni cuerpos del sitio.')
        return 1
    except Exception as error:
        emit('Resultado: diagnóstico interrumpido ('+type(error).__name__+'). Sin detalles privados.')
        return 1
    finally:
        await manager.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--merchant',default='')
    parser.add_argument('--steps',type=int,choices=range(1,9),default=8)
    parser.add_argument('--layout',action='store_true',help='Incluye visibilidad de enlaces con imágenes, sin valores, cookies ni consultas URL.')
    args=parser.parse_args()
    return asyncio.run(diagnose(args.url,args.merchant,args.steps,emit=lambda text:print(text,flush=True),layout=args.layout))


if __name__=='__main__':
    raise SystemExit(main())
