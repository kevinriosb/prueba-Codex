import asyncio
import ipaddress
import socket
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlsplit, urlunsplit

import httpx2


# Recursos de medición/publicidad, no portales ni bibliotecas de formularios.
# Comparar límites del hostname evita incluir dominios que solo imitan el nombre.
TRACKING_HOSTS=frozenset({'googletagmanager.com','google-analytics.com','doubleclick.net'})


def tracking_url(value):
    try:
        host=(urlsplit(value).hostname or '').lower().rstrip('.')
    except (TypeError,ValueError):
        return False
    return any(host==root or host.endswith('.'+root) for root in TRACKING_HOSTS)


class BrowserError(Exception):
    def __init__(self, code, message, status=409):
        self.code,self.message,self.status=code,message,status
        super().__init__(code)


def https_url(value):
    try:
        if not isinstance(value,str) or len(value)>2048 or any(ord(c)<33 or c=='\\' for c in value):
            raise ValueError()
        uri=urlsplit(value)
        host=(uri.hostname or '').encode('idna').decode().lower()
        if uri.scheme!='https' or not host or uri.username or uri.password or uri.port not in {None,443}:
            raise ValueError()
        if '.' not in host or host.endswith(('.local','.localhost','.internal')):
            raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError()
        except ValueError:
            if host.replace('.','').isdigit() or ':' in host:
                raise
        return urlunsplit(('https',host,uri.path or '/',uri.query,''))
    except (ValueError,UnicodeError):
        raise BrowserError('browser_url','Se requiere una dirección HTTPS pública válida.',422) from None


def origin(value):
    uri=urlsplit(https_url(value))
    return 'https://'+uri.hostname


def secure_link(value):
    """Un enlace HTTP observado solo se intenta por HTTPS en el mismo host."""
    uri=urlsplit(value)
    if uri.scheme=='http' and not uri.username and not uri.password and uri.port in {None,80}:
        value=urlunsplit(('https',uri.hostname,uri.path or '/',uri.query,uri.fragment))
    return https_url(value)


def public_page(value):
    uri=urlsplit(https_url(value))
    return urlunsplit((uri.scheme,uri.netloc,uri.path,'',''))


async def resolve(host):
    records=await asyncio.to_thread(socket.getaddrinfo,host,443,0,socket.SOCK_STREAM)
    return list(dict.fromkeys(item[4][0] for item in records))


class PublicFetcher:
    """Cada GET se conecta a una IP pública validada, conservando TLS Host/SNI."""
    def __init__(self, *, resolver=resolve, client=None):
        self.resolver=resolver
        self.client=client or httpx2.AsyncClient(trust_env=False,timeout=5,follow_redirects=False)
        # Las cookies pertenecen al contexto Chromium, nunca al transporte compartido.
        self.client.cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))

    async def close(self):
        await self.client.aclose()

    async def fetch(self,url,headers):
        uri=urlsplit(https_url(url))
        addresses=await self.resolver(uri.hostname)
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise BrowserError('browser_address','El destino no tiene exclusivamente direcciones públicas.',403)
        address=sorted(addresses,key=lambda value:':' in value)[0]
        authority='['+address+']' if ':' in address else address
        pinned=urlunsplit(('https',authority,uri.path,uri.query,''))
        forwarded={'Host':uri.hostname,'Accept':headers.get('accept','text/html'),
            'User-Agent':'PublicBillingNavigator/1.0','Cookie':headers.get('cookie','')}
        async with self.client.stream('GET',pinned,headers=forwarded,
                extensions={'sni_hostname':uri.hostname},follow_redirects=False) as response:
            chunks,size=[],0
            async for chunk in response.aiter_bytes():
                size+=len(chunk)
                if size>1_048_576:
                    raise BrowserError('browser_response_size','La página supera el tamaño permitido.')
                chunks.append(chunk)
            # Sin transfer/content-encoding: el cuerpo ya está descomprimido.
            allowed={'content-type','location','set-cookie','content-security-policy'}
            return response.status_code,{key:value for key,value in response.headers.items() if key.lower() in allowed},b''.join(chunks)
