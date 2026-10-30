import asyncio
import os
import secrets
import time
import uuid
import json
from contextlib import suppress
from html import escape
from dataclasses import dataclass, field
from urllib.parse import urljoin

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError

from .policy import BrowserError, PublicFetcher, https_url, origin, public_page, secure_link, tracking_url, bookmark_url, PAGE_REQUEST_LIMIT, ROUTE_REQUEST_LIMIT, PAGE_BYTE_LIMIT, ROUTE_BYTE_LIMIT, LEGACY_BYTE_LIMIT, RESPONSE_BYTE_LIMIT, network_failure
from .resources import PageResources,ResourceCache
from .filling import fill_fields,fill_status


# Código fijo del servidor. El modelo solo elige referencias observadas, nunca JS.
SNAPSHOT = r'''() => {
 const roots=[document];let traversalPartial=false;
 for(let i=0;i<roots.length&&i<16;i++) {
  const elements=Array.from(roots[i].querySelectorAll('*'));
  if(elements.length>2000)traversalPartial=true;
  for(const e of elements.slice(0,2000))if(e.shadowRoot){if(roots.length<16)roots.push(e.shadowRoot);else traversalPartial=true;}
 }
 const all=selector=>roots.flatMap(root=>Array.from(root.querySelectorAll(selector))).slice(0,2000);
 const visible=e=>!!(e.getClientRects().length)&&getComputedStyle(e).visibility!=='hidden';
 const text=roots.map(root=>root===document?(document.body?.innerText||''):(root.textContent||'')).join(' ').slice(0,40000).replace(/\s+/g,' ');
 const brand_selection=/(?:seleccion\w*|elige|elija|choose|select)\s+(?:[\p{L}\p{N}]+\s+){0,6}(?:marcas?|brands?|restaurantes?|establecimientos?|cadenas?|restaurants?|chains?)\b/iu.test(text);
 const labelText=e=>{const c=e.cloneNode(true);c.querySelectorAll('input,select,textarea,script,style').forEach(x=>x.remove());return c.textContent;};
 const label=e=>((e.labels&&Array.from(e.labels).map(labelText).join(' '))||e.getAttribute('aria-label')||e.innerText||e.getAttribute('placeholder')||e.getAttribute('name')||e.id||'').replace(/\s+/g,' ').trim().slice(0,100);
 const navigationLabel=e=>{
  const img=e.tagName==='IMG'||e.tagName==='INPUT'&&e.type==='image'?e:e.querySelector('img');
  let filename='';
  const src=img&&(img.getAttribute('src')||img.getAttribute('data-src'));
  if(src){try{filename=decodeURIComponent(new URL(src,document.baseURI).pathname.split('/').pop()).replace(/\.(?:png|svg|jpe?g|gif|webp)$/i,'').replace(/[_-]+/g,' ');}catch{}}
  return (e.getAttribute('aria-label')||e.getAttribute('title')||e.getAttribute('data-brand')||e.getAttribute('data-marca')||labelText(e).trim()||(img&&(img.alt||img.title))||e.querySelector('svg title')?.textContent||filename||publicBrand(e)||'').replace(/\s+/g,' ').trim().slice(0,100);
 };
 const literalNavigation=e=>{
  const code=(e.getAttribute('onclick')||'').trim();
  const patterns=[/^(?:return\s+)?(?:window\.)?open\(\s*(['"])([^'"\\]+)\1\s*(?:,\s*(['"])(?:_blank|_self)\3\s*)?\)\s*;?\s*(?:return\s+false;?)?$/,
   /^(?:(?:window|top)\.)?location(?:\.href)?\s*=\s*(['"])([^'"\\]+)\1\s*;?$/,
   /^(?:window\.)?location\.(?:assign|replace)\(\s*(['"])([^'"\\]+)\1\s*\)\s*;?$/];
  for(const pattern of patterns){const match=code.match(pattern);if(match){try{return new URL(match[2],document.baseURI).href;}catch{return null;}}}
  return null;
 };
 const publicBrand=e=>{
  if(!brand_selection)return '';
  try{
   const href=literalNavigation(e)||e.getAttribute('href');
   if(!href||href.startsWith('#'))return '';
   const url=new URL(href,document.baseURI);
   const names=new Set(['brand','marca','cadena','opc','opcion']);
   const values=Array.from(url.searchParams).filter(([key,value])=>names.has(key.toLowerCase())&&/^[a-z][a-z0-9 .’'-]{2,69}$/i.test(value)).map(([,value])=>value);
   return new Set(values).size===1?values[0]:'';
  }catch{return '';}
 };
 const inputs=all('input,select,textarea').filter(e=>visible(e)&&!e.disabled&&!['hidden','password','file','submit','button','reset','checkbox','radio'].includes(e.type));
 const fields=inputs.slice(0,24).map(e=>{
  const id='field-'+Math.random().toString(36).slice(2,12);e.setAttribute('data-public-field-ref',id);
  return {selector:id,signature:JSON.stringify([e.tagName,e.type,e.name,e.id,e.required,e.readOnly,e.disabled,e.maxLength,e.form?.getAttribute('action'),e.form?.getAttribute('method')]),
   label:label(e),kind:e.tagName.toLowerCase(),required:!!e.required||e.getAttribute('aria-required')==='true'||label(e).includes('*'),editable:!e.readOnly,
   input_type:e.type||'text',placeholder:(e.getAttribute('placeholder')||'').slice(0,100),
   max_length:e.maxLength>=0?e.maxLength:null,options:e.tagName==='SELECT'?Array.from(e.options).slice(0,25).map(o=>o.text.slice(0,70)):[]};
 });
 const unsupported_required=all('input').filter(e=>visible(e)&&e.required&&['file','checkbox','radio'].includes(e.type)).map(label).slice(0,10);
 const signature=e=>JSON.stringify(['href','onclick','aria-expanded','aria-selected','role','data-bs-toggle','data-toggle','data-bs-target','data-target'].map(name=>e.getAttribute(name)));
 const candidates=[];
 let scanned=0;
 for(const e of all('area[href],button[type=button],button:not([type]),input[type=button],[role=button],a[href],a[onclick],img[onclick],button[onclick],input[type=button][onclick],input[type=image][onclick],[role="link"][onclick],[role="button"][onclick],summary,[aria-expanded],[role="tab"],[data-bs-toggle="collapse"],[data-toggle="collapse"],[data-bs-toggle="tab"],[data-toggle="tab"],[data-bs-toggle="pill"],[data-toggle="pill"]')) {
  if(++scanned>800) break;
  if(!visible(e)||e.disabled) continue;
  const inForm=e.closest('form')||e.form;
  const literal=!inForm||e.tagName==='A'||e.type==='button'?literalNavigation(e):null;
  if(e.tagName==='BUTTON'&&e.type==='submit'&&!literal&&!e.hasAttribute('aria-expanded')&&e.getAttribute('role')!=='tab'
    &&!['collapse','tab','pill'].includes(e.getAttribute('data-bs-toggle'))&&!['collapse','tab','pill'].includes(e.getAttribute('data-toggle')))continue;
  const href=literal||(['A','AREA'].includes(e.tagName)?e.getAttribute('href'):null);
  const expand=(!href&&(e.tagName==='BUTTON'||e.type==='button'||e.getAttribute('role')==='button')&&(!inForm||e.type==='button'))||e.tagName==='SUMMARY'||e.hasAttribute('aria-expanded')||e.getAttribute('role')==='tab'||['collapse','tab','pill'].includes(e.getAttribute('data-bs-toggle'))||['collapse','tab','pill'].includes(e.getAttribute('data-toggle'));
  if(inForm&&!expand&&!literal&&e.tagName!=='A') continue;
  if(!expand&&(!href||href.startsWith('#'))) continue;
  if(expand&&(e.getAttribute('aria-selected')==='true'||e.getAttribute('aria-expanded')==='true')) continue;
  const text=navigationLabel(e);
  const brand=e.tagName==='IMG'||e.tagName==='INPUT'&&e.type==='image'||!!e.querySelector('img,svg')||!!e.getAttribute('data-brand')||!!e.getAttribute('data-marca')||!!publicBrand(e);
  const priority=/factur|invoic|billing|cfdi/i.test(text+' '+(href||''))?3:(brand?2:0);
  candidates.push({e,label:text,kind:expand?'expand':'link',url:expand?null:(literal||e.href),brand,priority,description:(e.closest('nav,footer,header,section,article,li,div')?.innerText||'').replace(/\s+/g,' ').trim().slice(0,140)});
 }
 // Selectores públicos: cada opción observada conserva su valor, sin inventar URLs.
 for(const select of all('select').filter(e=>visible(e)&&!e.disabled&&!e.closest('form'))) {
  for(const option of Array.from(select.options).slice(0,25)) {
   if(option.disabled||!option.value||option.selected)continue;
   candidates.push({e:option,label:option.text.slice(0,100),kind:'expand',url:null,brand:true,priority:1,
    choice:option.value,description:label(select)});
  }
 }
 const actions=[];
 for(const item of candidates.sort((a,b)=>b.priority-a.priority).slice(0,48)) {
  const id='nav-'+Math.random().toString(36).slice(2,12);
  const e=item.e;
  e.setAttribute('data-public-nav-ref',id);
  actions.push({selector:id,label:item.label,kind:item.kind,url:item.url,brand:item.brand,description:item.description,choice:item.choice,signature:signature(e)});
 }
 const login_required=all('input[type=password]').some(e=>visible(e)&&/iniciar sesi[oó]n|sign in|log in|login|contrase[nñ]a|password/i.test((e.closest('form')?.innerText||'')+' '+label(e)));
 return {title:document.title.slice(0,120),text:text.slice(0,900)+' '+text.slice(-600),fields,actions,brand_selection,login_required,unsupported_required,partial:traversalPartial||inputs.length>24||candidates.length>48||scanned>800,
  captcha:all('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],[class*="g-recaptcha"],[class*="h-captcha"]').some(visible)};
}'''


@dataclass
class Session:
    owner:str
    context:object
    page:object
    allowed:set
    public_route:bool=False
    created:float=field(default_factory=time.monotonic)
    observation:str=''
    actions:dict=field(default_factory=dict)
    field_controls:dict=field(default_factory=dict)
    private:bool=False
    private_blocked:int=0
    fill_result:dict|None=None
    lock:object=field(default_factory=asyncio.Lock)
    steps:int=1
    requests:int=0
    page_requests:int=0
    size:int=0
    blocked:set=field(default_factory=set)
    blocked_documents:set=field(default_factory=set)
    main_permission_origin:str|None=None
    network_blocked:bool=False
    unsafe_blocked:bool=False
    block_reason:str|None=None
    omitted_writes:int=0
    last_public_page:str=''
    resources:object=field(default_factory=PageResources)
    cache:object=field(default_factory=ResourceCache)
    cache_hits:int=0
    closed:bool=False
    captcha:bool=False
    login_required:bool=False
    request_pages:dict=field(default_factory=dict)
    transaction:dict=field(default_factory=dict)
    public_resources:set=field(default_factory=set)


class BrowserManager:
    def __init__(self, *, fetcher=None, executable='/usr/bin/chromium', ttl=180, max_steps=8):
        self.fetcher=fetcher or PublicFetcher()
        self.executable,self.ttl,self.max_steps=executable,ttl,max_steps
        self.sessions={}
        self.playwright=self.browser=None
        self.start_lock=asyncio.Lock()
        self.open_lock=asyncio.Lock()

    async def _start(self):
        async with self.start_lock:
            if self.browser:
                return
            driver=await async_playwright().start()
            try:
                browser=await driver.chromium.launch(executable_path=self.executable,headless=True,
                    env={name:os.environ[name] for name in ('PATH','LANG') if name in os.environ}|{'HOME':'/tmp','TMPDIR':'/tmp'},
                    args=['--proxy-server=http://127.0.0.1:9','--proxy-bypass-list=<-loopback>','--disable-dev-shm-usage',
                        '--disable-background-networking','--disable-quic','--disable-extensions',
                        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])
            except BaseException:
                await driver.stop()
                raise
            self.playwright,self.browser=driver,browser

    async def purge(self):
        for identifier,session in list(self.sessions.items()):
            if time.monotonic()-session.created>self.ttl:
                self.sessions.pop(identifier,None)
                await self._dispose(session)

    async def _dispose(self,session):
        session.closed=True
        session.cache.clear()
        session.request_pages.clear()
        getattr(session,'transaction',{}).clear()
        getattr(session,'public_resources',set()).clear()
        await session.context.close()

    async def close(self):
        sessions=list(self.sessions.values())
        self.sessions.clear()
        browser,driver=self.browser,self.playwright
        self.browser=self.playwright=None
        closers=[lambda session=session:self._dispose(session) for session in sessions]
        if browser:closers.append(browser.close)
        if driver:closers.append(driver.stop)
        closers.append(self.fetcher.close)
        first_error=None
        for close in closers:
            try:
                await close()
            except BaseException as error:
                if first_error is None:first_error=error
        if first_error is not None:raise first_error

    async def _route(self,session,route):
        if session.transaction.get('network'):
            from .submission import transaction_route
            return await transaction_route(self,session,route)
        request=route.request
        main_document=False
        page=session.resources
        try:
            if session.closed:
                await route.abort('blockedbyclient')
                return
            if session.private:
                session.private_blocked+=1
                await route.abort('blockedbyclient')
                return
            main_document=request.resource_type=='document' and request.frame==session.page.main_frame
            if main_document and request.method=='GET':
                session.page_requests=0
                session.resources=PageResources()
                page=session.resources
            session.request_pages[id(request)]=page
            if not main_document and tracking_url(request.url):
                await route.abort('blockedbyclient')
                return
            if request.method=='GET' and request.resource_type in {'image','font','media','other'}:
                await route.abort('blockedbyclient')
                return
            if session.public_route and request.method!='GET' and request.resource_type!='document':
                # Negar la petición auxiliar no invalida enlaces GET observados.
                # Nunca se reenvía su cuerpo, ni se asume que sea telemetría.
                session.omitted_writes+=1
                self._count_request(session,page)
                await route.abort('blockedbyclient')
                return
            url=https_url(request.url)
            if request.method!='GET' or request.resource_type not in {'document','script','stylesheet','xhr','fetch'}:
                session.block_reason='unsupported_request'
                raise ValueError()
            forwarded=await request.all_headers()
            for redirect in range(4):
                if session.private:
                    session.private_blocked+=1
                    await route.abort('blockedbyclient')
                    return
                if not main_document and tracking_url(url):
                    await route.abort('blockedbyclient')
                    return
                target=origin(url)
                self._count_request(session,page)
                if target not in session.allowed and not self._route_origin(session,target,document=request.resource_type=='document'):
                    await self._permission_block(session,route,url,main_document=main_document)
                    return
                cacheable=request.resource_type in {'script','stylesheet'}
                cached=session.cache.get(url,forwarded) if cacheable else None
                if cached:
                    status,headers,body=cached
                    session.cache_hits+=1
                    self._charge_bytes(session,page,len(body),network=False)
                elif hasattr(self.fetcher,'fetch_limited'):
                    status,headers,body=await self.fetcher.fetch_limited(url,forwarded,
                        on_bytes=lambda size:self._charge_bytes(session,page,size))
                else:
                    status,headers,body=await self.fetcher.fetch(url,forwarded)
                    self._charge_bytes(session,page,len(body))
                    if len(body)>RESPONSE_BYTE_LIMIT:
                        raise BrowserError('browser_response_size','Archivo demasiado grande.')
                if not cached and cacheable:
                    session.cache.put(url,forwarded,status,headers,body)
                if cacheable and status==200:
                    session.public_resources.add(url)
                    session.public_resources.add(request.url)
                if status>=400:
                    self._issue(page,request.resource_type,url,'http_error',status=status)
                    if main_document:
                        session.network_blocked=session.unsafe_blocked=True
                        session.block_reason='http_error'
                media=headers.get('content-type','').casefold()
                if main_document and media and not ('text/html' in media or 'application/xhtml+xml' in media):
                    session.network_blocked=session.unsafe_blocked=True
                    session.block_reason='unsupported_document'
                    self._issue(page,'document',url,'unsupported_document')
                    await route.abort('blockedbyclient')
                    return
                if request.resource_type in {'script','stylesheet'} and ('text/html' in media or 'application/json' in media):
                    self._issue(page,request.resource_type,url,'unexpected_content')
                if 'location' not in headers:
                    if redirect:
                        # La respuesta del proveedor final no puede instalar su
                        # cookie como si perteneciera al primer recurso del salto.
                        headers={name:value for name,value in headers.items() if name!='set-cookie'}
                    await route.fulfill(status=status,headers=headers,body=body)
                    return
                target_url=secure_link(urljoin(url,headers['location']))
                if not main_document and tracking_url(target_url):
                    await route.abort('blockedbyclient')
                    return
                if origin(target_url) not in session.allowed and not self._route_origin(session,origin(target_url),document=request.resource_type=='document'):
                    await self._permission_block(session,route,target_url,main_document=main_document,headers=headers)
                    return
                # Los documentos pasan por una referencia observada. Los recursos
                # del recorrido pasan por el mismo fetch con DNS/TLS en cada salto.
                if request.resource_type=='document':
                    await self._redirect_page(route,target_url,headers=headers)
                    return
                if headers.get('set-cookie'):
                    # No instalar cookies de un salto intermedio bajo el dominio
                    # de otro recurso. La navegación documental sí las conserva.
                    raise BrowserError('browser_redirect_cookie','Recurso redirigido con estado no compatible.')
                if not session.public_route or redirect==3:
                    raise ValueError()
                url=target_url
                # Jamás reenviar la cookie del origen anterior a otro proveedor.
                forwarded={key:value for key,value in forwarded.items() if key.lower()!='cookie'}
                if forwarded.get('referer'):
                    try:
                        forwarded['referer']=origin(forwarded['referer'])+'/'
                    except BrowserError:
                        forwarded.pop('referer',None)
        except Exception as error:
            if session.private:
                with suppress(PlaywrightError):await route.abort('blockedbyclient')
                return
            code={'browser_response_size':'response_size_limit','browser_address':'private_address','browser_redirect_cookie':'redirect_cookie'}.get(getattr(error,'code',''),getattr(error,'code',''))
            if code not in {'response_size_limit','private_address','size_limit','page_size_limit','redirect_cookie'}:
                code=network_failure(error)
            self._issue(page,request.resource_type,request.url,code)
            # Un script/estilo auxiliar puede fallar sin impedir enlaces visibles.
            # La navegación principal, métodos de escritura y límites sí detienen.
            if main_document or request.method!='GET' or session.unsafe_blocked:
                session.network_blocked=True
                session.unsafe_blocked=True
                session.block_reason=session.block_reason or code
            with suppress(PlaywrightError):
                await route.abort('blockedbyclient')

    def _issue(self,page,kind,url,code,*,status=None):
        try:
            site=origin(url)
        except BrowserError:
            site=''
        issue={'kind':kind,'site':site,'code':code,**({'status':status} if status else {})}
        if issue not in page.issues and len(page.issues)<12:
            page.issues.append(issue)

    def _charge_bytes(self,session,page,size,*,network=True):
        if session.closed:
            raise BrowserError('browser_session','La revisión terminó.',404)
        if session.private:
            raise BrowserError('browser_private','La red del llenado está cerrada.')
        if session.unsafe_blocked and session.block_reason in {'size_limit','page_size_limit'}:
            raise BrowserError(session.block_reason,'Presupuesto agotado.')
        if network:
            session.size+=size
        page.size+=size
        limit=ROUTE_BYTE_LIMIT if session.public_route else LEGACY_BYTE_LIMIT
        code='size_limit' if session.size>limit else ('page_size_limit' if session.public_route and page.size>PAGE_BYTE_LIMIT else None)
        if code:
            if code=='size_limit' or page is session.resources:
                session.unsafe_blocked=True
                session.block_reason=code
            raise BrowserError(code,'Se alcanzó el límite de bytes públicos.')

    def _count_request(self,session,page=None):
        if session.closed:
            raise BrowserError('browser_session','La revisión terminó.',404)
        page=page or session.resources
        session.requests+=1
        page.requests+=1
        if page is session.resources:
            session.page_requests=page.requests
        total_limit=ROUTE_REQUEST_LIMIT if session.public_route else PAGE_REQUEST_LIMIT
        reason='request_limit' if session.requests>total_limit else (
            'page_request_limit' if session.public_route and page.requests>PAGE_REQUEST_LIMIT else None)
        if reason:
            if reason=='request_limit' or page is session.resources:
                session.unsafe_blocked=True
                session.block_reason=reason
            raise BrowserError('browser_requests','Se alcanzó el límite de peticiones públicas.')

    def _route_origin(self,session,target,*,document=False):
        # El permiso de recorrido habilita el presupuesto, nunca nuevos dominios.
        # Documentos, iframes y recursos deben proceder de permisos explícitos.
        if not session.public_route:return False
        if len(session.allowed)>=12:
            session.unsafe_blocked=True
            session.block_reason='origin_limit'
            raise BrowserError('browser_origin','Se alcanzó el límite de destinos del recorrido público.')
        return False

    async def _permission_block(self,session,route,url,*,main_document,headers=None):
        session.blocked.add(origin(url))
        if route.request.resource_type=='document':session.blocked_documents.add(origin(url))
        if main_document:
            session.main_permission_origin=origin(url)
            session.network_blocked=True
            session.block_reason='permission'
            await self._redirect_page(route,url,headers=headers)
        else:
            await route.abort('blockedbyclient')

    async def _redirect_page(self,route,url,*,headers=None):
        # Sin meta-refresh ni JS: el siguiente paso requiere una referencia nueva.
        await route.fulfill(status=200,content_type='text/html',headers={
            'set-cookie':headers['set-cookie']} if headers and headers.get('set-cookie') else {},body=
            '<!doctype html><title>Continuar navegación</title><a href="'+escape(url,quote=True)+
            '">Continuar al portal de facturación</a>')

    async def open(self,owner,url,allowed,*,public_route=False):
        if self.open_lock.locked():
            raise BrowserError('browser_busy','El navegador está abriendo otra página.',429)
        async with self.open_lock:
            return await self._open(owner,url,allowed,public_route=public_route)

    async def _open(self,owner,url,allowed,*,public_route=False):
        url=https_url(url)
        allowed={origin(item) for item in allowed}
        if not allowed or len(allowed)>12 or origin(url) not in allowed:
            raise BrowserError('browser_origin','El operador debe autorizar este dominio antes de navegar.',403)
        await self.purge()
        if len(self.sessions)>=2:
            raise BrowserError('browser_busy','El navegador está ocupado. Intenta más tarde.',429)
        await self._start()
        context=await self.browser.new_context(service_workers='block',accept_downloads=False,
            java_script_enabled=True,ignore_https_errors=False)
        context.set_default_timeout(4000)
        await context.route_web_socket('**/*',lambda socket:socket.close())
        page=await context.new_page()
        context.on('page',lambda popup:popup.close())
        session=Session(owner,context,page,allowed,public_route=public_route,last_public_page=public_page(url))
        def finished(request):
            session.request_pages.pop(id(request),None)
        def failed(request):
            resource_page=session.request_pages.pop(id(request),session.resources)
            if session.closed or session.private or request.method!='GET' or request.resource_type not in {'document','script','stylesheet','xhr','fetch'} or tracking_url(request.url):
                return
            if request.failure and ('BLOCKED_BY_CLIENT' in request.failure or 'ERR_ABORTED' in request.failure):
                return
            self._issue(resource_page,request.resource_type,request.url,'load_failed')
        def console(message):
            if not session.closed and not session.private and message.type=='error':
                text=message.text[:10000].casefold()
                code=('csp' if 'content security policy' in text else 'cors' if 'cors policy' in text
                    else 'unexpected_content' if 'mime type' in text else None)
                if code:self._issue(session.resources,'script',page.url,code)
        page.on('requestfinished',finished)
        page.on('requestfailed',failed)
        page.on('console',console)
        page.on('pageerror',lambda _error:self._issue(session.resources,'script',page.url,'javascript_error') if not session.closed else None)
        await context.route('**/*',lambda route:self._route(session,route))
        page.on('dialog',lambda dialog:dialog.dismiss())
        identifier=str(uuid.uuid4())
        self.sessions[identifier]=session
        try:
            async with asyncio.timeout(10):
                await page.goto(url,wait_until='domcontentloaded',timeout=8000)
                return await self._observe(identifier,session)
        except Exception:
            if session.network_blocked:
                try:
                    return await self._observe(identifier,session)
                except Exception:
                    pass
            self.sessions.pop(identifier,None)
            await self._dispose(session)
            raise BrowserError('browser_navigation','No se pudo abrir la página pública autorizada.',502) from None

    async def get(self,owner,identifier):
        await self.purge()
        session=self.sessions.get(identifier)
        if not session or session.owner!=owner:
            raise BrowserError('browser_session','La revisión no existe o venció. Abre otra revisión.',404)
        return session

    async def observe(self,owner,identifier):
        session=await self.get(owner,identifier)
        async with session.lock:
            return await self._observe(identifier,session)

    async def _observe(self,identifier,session,*,transaction=False):
        if session.private and not transaction:
            raise BrowserError('browser_private','El llenado es privado; no se devuelve al modelo ni a la navegación pública.')
        session.observation=secrets.token_hex(4)
        session.actions={}
        session.field_controls={}
        fields,actions=[],[]
        unsupported_required=[]
        title=''
        page_text=''
        captcha=brand_selection=login_required=partial=False
        async with asyncio.timeout(6):
            # El DOM principal puede estar listo antes del formulario de un iframe.
            try:
                await session.page.wait_for_load_state('load',timeout=1000)
            except PlaywrightTimeout:
                pass
            eligible=[]
            partial=len(session.page.frames)>64
            for frame in session.page.frames[:64]:
                source=frame.url
                ancestor=frame
                for _ in range(4):
                    if source not in {'about:blank','about:srcdoc'} or not ancestor.parent_frame:
                        break
                    ancestor=ancestor.parent_frame
                    source=ancestor.url
                try:
                    approved=origin(source) in session.allowed
                except BrowserError:
                    approved=False
                if not approved:
                    continue
                if frame!=session.page.main_frame:
                    try:
                        if not await (await frame.frame_element()).is_visible():continue
                    except PlaywrightError:
                        partial=True
                        continue
                eligible.append((frame,source))
            partial=partial or len(eligible)>8
            selector_waited=False
            for frame,source in eligible[:8]:
                try:
                    result=await frame.evaluate(SNAPSHOT)
                    if (not selector_waited and result['brand_selection'] and not result['fields']
                            and len({item['label'].casefold() for item in result['actions'] if item['brand'] and item['label']})<2):
                        selector_waited=True
                        # Una sola espera DOM acotada, sin otra ejecución de IA.
                        try:
                            await frame.wait_for_function('() => {const s=('+SNAPSHOT+')();return s.fields.length || new Set(s.actions.filter(a=>a.brand&&a.label).map(a=>a.label.toLowerCase())).size>=2;}',polling=100,timeout=1200)
                        except PlaywrightTimeout:
                            pass
                        # La espera renueva marcadores; conservar solo los últimos.
                        result=await frame.evaluate(SNAPSHOT)
                except PlaywrightError:
                    partial=True
                    continue
                title=title or result['title']
                page_text=(page_text+' '+result.get('text',''))[:1500]
                captcha=captcha or result['captcha']
                brand_selection=brand_selection or result['brand_selection']
                login_required=login_required or result['login_required']
                partial=partial or result['partial']
                unsupported_required.extend(result['unsupported_required'])
                for item in result['fields']:
                    reference=secrets.token_hex(4)
                    session.field_controls[reference]=(frame,item|{'frame_url':frame.url})
                    fields.append({key:value for key,value in item.items() if key not in {'selector','signature'}}|
                        {'source_url':bookmark_url(source),'reference':reference})
                for item in result['actions']:
                    permission_origin=None
                    if item['kind']=='link':
                        try:
                            url=secure_link(item['url'])
                            if tracking_url(url):
                                continue
                            if url==secure_link(source):
                                continue
                            if origin(url) not in session.allowed:
                                permission_origin=origin(url)
                            item['url']=url
                        except BrowserError:
                            continue
                    reference=secrets.token_hex(4)
                    session.actions[reference]=(frame,item)
                    actions.append({'reference':reference,'label':item['label'],'kind':item['kind'],'brand':item['brand'],'description':item.get('description',''),
                        **({'permission_origin':permission_origin} if permission_origin else {})})
        try:
            page=public_page(session.page.url)
            bookmark=bookmark_url(session.page.url)
            session.last_public_page=page
        except BrowserError:
            if not session.network_blocked:
                raise
            # Chromium puede sustituir una navegación denegada por chrome-error.
            # Conservar la dirección pública previa y la causa, sin inventar DOM.
            page,bookmark=session.last_public_page,None
        session.captcha,session.login_required=captcha,login_required
        result={'session_id':identifier,'observation_id':session.observation,'page':page,
            'bookmark_url':bookmark,
            'title':title,'text':page_text,'fields':fields[:24],'actions':actions[:48],'captcha':captcha,
            'login_required':login_required,'partial':partial or len(fields)>24 or len(actions)>48,
            'unsupported_required':unsupported_required[:10],
            'blocked_origins':sorted(session.blocked)[:12],'network_blocked':session.network_blocked,
            'block_reason':session.block_reason,'omitted_writes':session.omitted_writes,'brand_selection':brand_selection,
            'requests_remaining':max(0,(ROUTE_REQUEST_LIMIT if session.public_route else PAGE_REQUEST_LIMIT)-session.requests),
            'page_requests_remaining':max(0,PAGE_REQUEST_LIMIT-session.page_requests),
            'steps_remaining':max(0,self.max_steps-session.steps),
            'public_only':not session.private,'purchase_validated':False,'submitted':False,
            'diagnostics':{'requests':session.requests,'page_requests':session.resources.requests,
                'transferred_bytes':session.size,'page_bytes':session.resources.size,
                'cache_hits':session.cache_hits,'cache_bytes':session.cache.size,'issues':list(session.resources.issues)}}
        # Los valores de inputs nunca entran aquí. Reducir opciones antes de
        # superar el contrato de 64 KiB del cliente, marcando la vista parcial.
        while len(json.dumps(result,ensure_ascii=False).encode())>60000:
            options=next((item for item in reversed(result['fields']) if item['options']),None)
            if options:
                options['options'].pop()
            elif result['actions']:
                result['actions'].pop()
            elif result['fields']:
                result['fields'].pop()
            else:
                break
            result['partial']=True
        return result

    async def act(self,owner,identifier,observation_id,reference):
        session=await self.get(owner,identifier)
        async with session.lock:
            if session.private:
                raise BrowserError('browser_private','La sesión de llenado no admite navegación pública.')
            if observation_id!=session.observation or reference not in session.actions:
                raise BrowserError('browser_stale','La página cambió. Revisa el estado antes de continuar.')
            if session.network_blocked:
                raise BrowserError('browser_intervention','Se bloqueó una solicitud de la página. Requiere intervención.')
            if session.captcha or session.login_required:
                raise BrowserError('browser_intervention','El sitio requiere intervención humana.')
            if session.steps>=self.max_steps:
                raise BrowserError('browser_steps','La revisión alcanzó su límite de navegación. Requiere revisión humana.')
            frame,item=session.actions[reference]
            if item['kind']=='link' and origin(item['url']) not in session.allowed:
                raise BrowserError('browser_origin','Autoriza el nuevo dominio antes de abrirlo.',403)
            session.actions.pop(reference)
            session.observation=''
            session.steps+=1  # Consumir ANTES del clic: no se reintenta por fallos.
            locator=frame.locator('[data-public-nav-ref="'+item['selector']+'"]')
            try:
                valid=await locator.count()==1 and await locator.evaluate(
                    "e=>JSON.stringify(['href','onclick','aria-expanded','aria-selected','role','data-bs-toggle','data-toggle','data-bs-target','data-target'].map(name=>e.getAttribute(name)))")==item['signature']
            except PlaywrightError:
                valid=False
            if not valid:
                raise BrowserError('browser_stale','El control cambió. Revisa la página nuevamente.')
            if item['kind']=='link':
                await session.page.goto(https_url(item['url']),wait_until='domcontentloaded',timeout=8000)
            elif item.get('choice') is not None:
                # onchange puede iniciar navegación después de select_option;
                # observar el contexto anterior devuelve un falso DOM vacío.
                with suppress(PlaywrightTimeout):
                    async with session.page.expect_navigation(wait_until='domcontentloaded',timeout=1200):
                        await locator.locator('..').select_option(item['choice'],timeout=4000)
            else:
                await locator.click(timeout=4000)
            return await self._observe(identifier,session)

    async def allow(self,owner,identifier,allowed,*,public_route=False):
        session=await self.get(owner,identifier)
        async with session.lock:
            if session.private:
                raise BrowserError('browser_private','La sesión de llenado conserva la red cerrada.')
            allowed={origin(value) for value in allowed}
            if public_route:
                allowed|=session.allowed
            if not allowed or len(allowed)>12 or not session.allowed<=allowed:
                raise BrowserError('browser_origin','Permisos de revisión no válidos.',403)
            session.allowed=allowed
            session.public_route=session.public_route or public_route
            newly_allowed=session.blocked & allowed
            session.blocked.difference_update(allowed)
            session.blocked_documents.difference_update(allowed)
            main_allowed=not session.main_permission_origin or session.main_permission_origin in allowed
            if newly_allowed and main_allowed and not session.unsafe_blocked:
                session.network_blocked=False
                session.block_reason=None
                session.main_permission_origin=None
                # Recargar solo la página ya autorizada; nunca enviar formularios.
                await session.page.reload(wait_until='domcontentloaded',timeout=8000)
            return await self._observe(identifier,session)

    async def close_session(self,owner,identifier):
        session=await self.get(owner,identifier)
        async with session.lock:
            self.sessions.pop(identifier,None)
            await self._dispose(session)
        return {'closed':True}

    async def fill(self,owner,identifier,observation_id,values):
        return await fill_fields(self,owner,identifier,observation_id,values)

    async def filled(self,owner,identifier):
        return await fill_status(self,owner,identifier)

    async def transaction_view(self,owner,identifier):
        from .submission import transaction_view
        session=await self.get(owner,identifier)
        async with session.lock:
            return await transaction_view(self,identifier,session)

    async def transaction_send(self,owner,identifier,**payload):
        from .submission import transaction_send
        return await transaction_send(self,owner,identifier,**payload)

    async def transaction_file(self,owner,identifier,reference):
        session=await self.get(owner,identifier)
        async with session.lock:
            item=session.transaction.get('files',{}).get(reference)
            if not item:raise BrowserError('browser_session','Archivo no disponible.',404)
            return item

    async def transaction_status(self,owner,identifier,operation_id):
        from .submission import transaction_status
        return await transaction_status(self,owner,identifier,operation_id)
