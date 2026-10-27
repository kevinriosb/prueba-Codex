"""Diagnóstico por stdin en los contenedores existentes: sin IA ni datos fiscales.

connection se ejecuta en agent; portal en browser. No modifica configuración,
registros o documentos. El portal solo recibe GET públicos y no se siguen 30x.
"""
import argparse
import asyncio
import json
import os
import socket
import ssl
from contextlib import suppress
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


MESSAGES={
    'dns':'DNS: el nombre del portal no se pudo resolver desde el contenedor.',
    'tls':'TLS: falló la validación del certificado HTTPS del portal.',
    'timeout':'Red: el portal no respondió dentro del tiempo de espera.',
    'connection':'Red: no se pudo establecer la conexión HTTPS con el portal.',
    'browser_address':'Seguridad: el DNS incluye direcciones privadas o no permitidas.',
    'browser_response_size':'La página excede el límite de tamaño del navegador.',
    'chromium':'Chromium: no pudo iniciar o ejecutar la página de prueba local.',
    'unknown':'El diagnóstico no identificó la causa; no se mostrarán detalles privados.',
}


def category(error):
    """Clasificar sin imprimir excepciones, URL, cabeceras o cuerpos del proveedor."""
    causes=[]
    current=error
    for _ in range(10):
        if current is None or any(current is value for value in causes):
            break
        causes.append(current)
        current=current.__cause__ or current.__context__
    if any(isinstance(value,socket.gaierror) for value in causes):
        return 'dns'
    if any(isinstance(value,ssl.SSLError) or 'CERTIFICATE_VERIFY_FAILED' in str(value) for value in causes):
        return 'tls'
    if any(isinstance(value,TimeoutError) or type(value).__name__ in {'ReadTimeout','ConnectTimeout','PoolTimeout','WriteTimeout'} for value in causes):
        return 'timeout'
    for value in causes:
        if getattr(value,'code',None) in MESSAGES:
            return value.code
    if any(isinstance(value,OSError) or type(value).__name__ in {'ConnectError','NetworkError','ReadError'} for value in causes):
        return 'connection'
    return 'unknown'


def metadata(opener,url,*,token=None,payload=None):
    headers={'Accept':'application/json'}
    if token:
        headers['X-Browser-Key']=token
    if payload is not None:
        headers['Content-Type']='application/json'
    request=Request(url,headers=headers,data=json.dumps(payload).encode() if payload is not None else None)
    try:
        response=opener.open(request,timeout=10)
    except HTTPError as error:
        response=error
    with response:
        status=response.code
        raw=response.read(8193)
    if len(raw)>8192:
        return status,{}
    try:
        body=json.loads(raw)
    except (ValueError,UnicodeError):
        body={}
    return status,body if isinstance(body,dict) else {}


def check_connection(environment,*,opener=None,emit=print):
    opener=opener or build_opener(ProxyHandler({}),NoRedirects())
    endpoint=environment.get('BILLING_BROWSER_WORKER_URL','http://browser:8001').rstrip('/')
    try:
        uri=urlsplit(endpoint)
        if uri.scheme not in {'http','https'} or not uri.hostname or uri.username or uri.password or uri.query or uri.fragment:
            raise ValueError()
        status,body=metadata(opener,endpoint+'/health')
        if status!=200 or body.get('status')!='ok' or body.get('public_only') is not True:
            emit('Conexión interna: /health no confirmó el navegador esperado.')
            return 1
        emit('Conexión interna: navegador disponible.')
        token=Path(environment.get('BILLING_BROWSER_TOKEN_FILE','/run/browser-auth/token')).read_text().strip()
        if len(token)<40:
            raise ValueError()
        status,body=metadata(opener,endpoint+'/v1/sessions',token=token,payload={
            'owner':'diagnostic-no-user','url':'file:///diagnostic-not-opened','allowed':['https://diagnostic.invalid']})
        if status==422 and body.get('code')=='browser_url':
            emit('Autenticación interna: correcta. La prueba fue rechazada antes de abrir páginas.')
            return 0
        if status in {401,403}:
            emit('Autenticación interna: rechazada. Los servicios no reconocen la misma credencial privada.')
        else:
            emit('API interna: respuesta inesperada; comprueba que agent y browser tengan la misma actualización.')
    except (OSError,ValueError):
        emit('Conexión/configuración interna: falló. Comprueba los servicios y el volumen browser_auth; no se muestran claves.')
    return 1


class LocalPage:
    async def fetch(self,url,headers):
        return 200,{'content-type':'text/html'},b'<title>Local</title><label>Cuenta<input></label>'
    async def close(self):
        pass


async def check_chromium(*,manager_factory=None,emit=print):
    if manager_factory is None:
        from backend.browser_worker.engine import BrowserManager
        manager_factory=BrowserManager
    manager=manager_factory(fetcher=LocalPage())
    try:
        snapshot=await manager.open('diagnostic-local','https://diagnostic.invalid/',['https://diagnostic.invalid'])
        if not snapshot['fields']:
            raise ValueError()
        emit('Chromium: inicio y lectura de la página local correctos; sin conexión a comercios.')
        return 0
    except Exception:
        emit(MESSAGES['chromium'])
        return 1
    finally:
        with suppress(Exception):
            await manager.close()


async def check_portal(value,*,fetcher_factory=None,emit=print):
    from backend.browser_worker.policy import PublicFetcher,origin,secure_link
    try:
        uri=urlsplit(secure_link(value))
        url=urlunsplit((uri.scheme,uri.netloc,uri.path or '/','',''))
    except Exception:
        emit('Portal: dirección no permitida. Se requiere un portal público con HTTPS.')
        return 1
    emit('Portal comprobado: '+origin(url)+' (sin consultas ni fragmentos).')
    fetcher=(fetcher_factory or PublicFetcher)()
    try:
        status,headers,_body=await asyncio.wait_for(fetcher.fetch(url,{}),timeout=12)
        if 300<=status<400 and headers.get('location'):
            from urllib.parse import urljoin
            try:
                target=origin(secure_link(urljoin(url,headers['location'])))
            except Exception:
                emit('Portal: redirección no permitida. No se siguió.')
                return 1
            emit('HTTPS: respondió HTTP '+str(status)+'. Redirige a '+target+'. No se abrió el destino.')
            return 0
        if status==200:
            emit('HTTPS: respondió HTTP 200. Esto no confirma que el formulario sea compatible ni valida el ticket.')
            return 0
        reasons={403:'el sitio rechazó el acceso',404:'no existe esa dirección',429:'el sitio limitó las consultas'}
        emit('HTTPS: HTTP '+str(status)+'; '+reasons.get(status,'respuesta del servidor sin éxito')+'.')
        return 1
    except Exception as error:
        emit(MESSAGES[category(error)])
        return 1
    finally:
        with suppress(Exception):
            await fetcher.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component',required=True,choices=['connection','portal'])
    parser.add_argument('--url')
    args=parser.parse_args(argv)
    print('Diagnóstico sin IA ni datos fiscales. No modifica perfiles, tickets, cuotas o configuración.')
    if args.component=='connection':
        return check_connection(os.environ)
    if not args.url:
        parser.error('--url es obligatorio para comprobar el portal')
    async def run():
        # El GET HTTPS es independiente: informar ambas etapas incluso si Chromium falla.
        chromium=await check_chromium()
        portal=await check_portal(args.url)
        return max(chromium,portal)
    try:
        return asyncio.run(run())
    except ImportError:
        print('Imagen incompleta: ejecuta este componente dentro de browser, no en el Python de AlmaLinux.')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
