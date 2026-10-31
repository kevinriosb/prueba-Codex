"""Precomprobación pública sin observar campos, cookies ni seguir redirecciones."""
from ...browser_worker.policy import PublicFetcher, secure_link, bookmark_url, BrowserError, network_failure


async def probe_url(value,*,fetcher=None):
    result={'url':value,'reachable':False,'reason':'invalid_url','status':None}
    own=fetcher is None
    transport=fetcher or PublicFetcher()
    try:
        # Reutiliza DNS público, IP fijada, TLS, timeout y tamaño del navegador.
        url=bookmark_url(secure_link(value))
        if not url:return result
        result['url']=url
        status,_,_=await transport.fetch(url,{'accept':'text/html'})
        result.update(status=status,reachable=200<=status<400,reason=None if 200<=status<400 else 'http_error')
    except Exception as error:
        result['reason']='private_address' if isinstance(error,BrowserError) and error.code=='browser_address' else network_failure(error)
    finally:
        if own:await transport.close()
    return result
