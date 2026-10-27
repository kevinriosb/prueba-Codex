import asyncio
import os
import secrets
import time
import uuid
from html import escape
from dataclasses import dataclass, field
from urllib.parse import urljoin

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from .policy import BrowserError, PublicFetcher, https_url, origin, public_page, secure_link, tracking_url, bookmark_url


# Código fijo del servidor. El modelo solo elige referencias observadas, nunca JS.
SNAPSHOT = r'''() => {
 const visible=e=>!!(e.getClientRects().length)&&getComputedStyle(e).visibility!=='hidden';
 const labelText=e=>{const c=e.cloneNode(true);c.querySelectorAll('input,select,textarea,script,style').forEach(x=>x.remove());return c.textContent;};
 const label=e=>((e.labels&&Array.from(e.labels).map(labelText).join(' '))||e.getAttribute('aria-label')||e.innerText||e.getAttribute('placeholder')||e.getAttribute('name')||e.id||'').replace(/\s+/g,' ').trim().slice(0,100);
 const navigationLabel=e=>{
  const img=e.tagName==='IMG'?e:e.querySelector('img');
  let filename='';
  if(img){try{filename=decodeURIComponent(new URL(img.getAttribute('src')||'',document.baseURI).pathname.split('/').pop()).replace(/\.(?:png|svg|jpe?g|gif|webp)$/i,'').replace(/[_-]+/g,' ');}catch{}}
  return (e.getAttribute('aria-label')||e.getAttribute('title')||labelText(e).trim()||(img&&(img.alt||img.title))||filename||'').replace(/\s+/g,' ').trim().slice(0,100);
 };
 const literalNavigation=e=>{
  const code=(e.getAttribute('onclick')||'').trim();
  const patterns=[/^(?:return\s+)?(?:window\.)?open\(\s*(['"])([^'"\\]+)\1\s*(?:,\s*(['"])(?:_blank|_self)\3\s*)?\)\s*;?\s*(?:return\s+false;?)?$/,
   /^(?:(?:window|top)\.)?location(?:\.href)?\s*=\s*(['"])([^'"\\]+)\1\s*;?$/,
   /^(?:window\.)?location\.(?:assign|replace)\(\s*(['"])([^'"\\]+)\1\s*\)\s*;?$/];
  for(const pattern of patterns){const match=code.match(pattern);if(match){try{return new URL(match[2],document.baseURI).href;}catch{return null;}}}
  return null;
 };
 const fields=Array.from(document.querySelectorAll('input,select,textarea')).filter(e=>visible(e)&&!e.disabled&&!['hidden','password','file','submit','button','reset'].includes(e.type)).slice(0,24).map(e=>({label:label(e),kind:e.tagName.toLowerCase(),required:!!e.required,options:e.tagName==='SELECT'?Array.from(e.options).slice(0,25).map(o=>o.text.slice(0,70)):[]}));
 const candidates=[];
 let scanned=0;
 for(const e of document.querySelectorAll('a[href],a[onclick],img[onclick],button[onclick],[role="link"][onclick],[role="button"][onclick],summary,[aria-expanded],[role="tab"],[data-bs-toggle="collapse"],[data-toggle="collapse"]')) {
  if(++scanned>800) break;
  if(!visible(e)||e.disabled) continue;
  const inForm=e.closest('form')||e.form;
  const literal=inForm?null:literalNavigation(e);
  const href=literal||(e.tagName==='A'?e.getAttribute('href'):null);
  const expand=e.tagName==='SUMMARY'||e.hasAttribute('aria-expanded')||e.getAttribute('role')==='tab'||e.getAttribute('data-bs-toggle')==='collapse'||e.getAttribute('data-toggle')==='collapse';
  if(inForm&&!expand) continue;
  if(!expand&&(!href||href.startsWith('#'))) continue;
  if(expand&&(e.getAttribute('aria-selected')==='true'||e.getAttribute('aria-expanded')==='true')) continue;
  const text=navigationLabel(e);
  const brand=e.tagName==='IMG'||!!e.querySelector('img');
  const priority=/factur|invoic|billing|cfdi/i.test(text+' '+(href||''))?3:(brand?2:0);
  candidates.push({e,label:text,kind:expand?'expand':'link',url:expand?null:(literal||e.href),brand,priority});
 }
 const actions=[];
 for(const item of candidates.sort((a,b)=>b.priority-a.priority).slice(0,24)) {
  const id='nav-'+Math.random().toString(36).slice(2,12);
  const e=item.e;
  e.setAttribute('data-public-nav-ref',id);
  actions.push({selector:id,label:item.label,kind:item.kind,url:item.url,brand:item.brand});
 }
 return {title:document.title.slice(0,120),fields,actions,captcha:!!document.querySelector('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],[class*="g-recaptcha"],[class*="h-captcha"]')};
}'''


@dataclass
class Session:
    owner:str
    context:object
    page:object
    allowed:set
    created:float=field(default_factory=time.monotonic)
    observation:str=''
    actions:dict=field(default_factory=dict)
    lock:object=field(default_factory=asyncio.Lock)
    steps:int=1
    requests:int=0
    size:int=0
    blocked:set=field(default_factory=set)
    network_blocked:bool=False
    unsafe_blocked:bool=False


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
            self.playwright=await async_playwright().start()
            self.browser=await self.playwright.chromium.launch(executable_path=self.executable,headless=True,
                env={name:os.environ[name] for name in ('PATH','LANG') if name in os.environ}|{'HOME':'/tmp','TMPDIR':'/tmp'},
                args=['--proxy-server=http://127.0.0.1:9','--proxy-bypass-list=<-loopback>','--disable-dev-shm-usage',
                    '--disable-background-networking','--disable-quic','--disable-extensions',
                    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])

    async def purge(self):
        for identifier,session in list(self.sessions.items()):
            if time.monotonic()-session.created>self.ttl:
                self.sessions.pop(identifier,None)
                await session.context.close()

    async def close(self):
        for session in list(self.sessions.values()):
            await session.context.close()
        self.sessions.clear()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        await self.fetcher.close()

    async def _route(self,session,route):
        request=route.request
        main_document=False
        try:
            main_document=request.resource_type=='document' and request.frame==session.page.main_frame
            if not main_document and tracking_url(request.url):
                await route.abort('blockedbyclient')
                return
            if request.method=='GET' and request.resource_type in {'image','font','media','other'}:
                await route.abort('blockedbyclient')
                return
            url=https_url(request.url)
            target=origin(url)
            if request.method!='GET' or request.resource_type not in {'document','script','stylesheet','xhr','fetch'}:
                raise ValueError()
            session.requests+=1
            if session.requests>60:
                session.unsafe_blocked=True
                raise ValueError()
            if target not in session.allowed:
                await self._permission_block(session,route,url,main_document=main_document)
                return
            status,headers,body=await self.fetcher.fetch(url,await request.all_headers())
            session.size+=len(body)
            if session.size>6_291_456:
                session.unsafe_blocked=True
                raise ValueError()
            if 'location' in headers:
                target_url=secure_link(urljoin(url,headers['location']))
                if origin(target_url) not in session.allowed:
                    if not main_document and tracking_url(target_url):
                        await route.abort('blockedbyclient')
                    else:
                        await self._permission_block(session,route,target_url,main_document=main_document)
                    return
                # Chromium no enruta todos los saltos 30x por Playwright. Convertirlos
                # en una referencia observada garantiza que cada GET use el filtro.
                if request.resource_type=='document':
                    await self._redirect_page(route,target_url)
                    return
                raise ValueError()  # Recurso redirigido: intervención, sin salida directa.
            await route.fulfill(status=status,headers=headers,body=body)
        except Exception:
            # Un script/estilo auxiliar puede fallar sin impedir enlaces visibles.
            # La navegación principal, métodos de escritura y límites sí detienen.
            if main_document or request.method!='GET' or session.unsafe_blocked:
                session.network_blocked=True
                session.unsafe_blocked=True
            await route.abort('blockedbyclient')

    async def _permission_block(self,session,route,url,*,main_document):
        session.blocked.add(origin(url))
        if main_document:
            session.network_blocked=True
            await self._redirect_page(route,url)
        else:
            await route.abort('blockedbyclient')

    async def _redirect_page(self,route,url):
        # Sin meta-refresh ni JS: el siguiente paso requiere una referencia nueva.
        await route.fulfill(status=200,content_type='text/html',body=
            '<!doctype html><title>Continuar navegación</title><a href="'+escape(url,quote=True)+
            '">Continuar al portal de facturación</a>')

    async def open(self,owner,url,allowed):
        if self.open_lock.locked():
            raise BrowserError('browser_busy','El navegador está abriendo otra página.',429)
        async with self.open_lock:
            return await self._open(owner,url,allowed)

    async def _open(self,owner,url,allowed):
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
        session=Session(owner,context,page,allowed)
        await context.route('**/*',lambda route:self._route(session,route))
        page.on('dialog',lambda dialog:dialog.dismiss())
        identifier=str(uuid.uuid4())
        self.sessions[identifier]=session
        try:
            async with asyncio.timeout(10):
                await page.goto(url,wait_until='domcontentloaded',timeout=8000)
                return await self._observe(identifier,session)
        except Exception:
            self.sessions.pop(identifier,None)
            await context.close()
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

    async def _observe(self,identifier,session):
        session.observation=secrets.token_hex(4)
        session.actions={}
        fields,actions=[],[]
        title=''
        captcha=False
        async with asyncio.timeout(6):
            # El DOM principal puede estar listo antes del formulario de un iframe.
            try:
                await session.page.wait_for_load_state('load',timeout=1000)
            except PlaywrightTimeout:
                pass
            for frame in session.page.frames[:4]:
                try:
                    approved=origin(frame.url) in session.allowed
                except BrowserError:
                    approved=False
                if not approved:
                    continue
                result=await frame.evaluate(SNAPSHOT)
                title=title or result['title']
                captcha=captcha or result['captcha']
                fields.extend(item|{'source_url':bookmark_url(frame.url)} for item in result['fields'])
                for item in result['actions']:
                    permission_origin=None
                    if item['kind']=='link':
                        try:
                            url=secure_link(item['url'])
                            if tracking_url(url):
                                continue
                            if url==secure_link(frame.url):
                                continue
                            if origin(url) not in session.allowed:
                                permission_origin=origin(url)
                            item['url']=url
                        except BrowserError:
                            continue
                    reference=secrets.token_hex(4)
                    session.actions[reference]=(frame,item)
                    actions.append({'reference':reference,'label':item['label'],'kind':item['kind'],'brand':item['brand'],
                        **({'permission_origin':permission_origin} if permission_origin else {})})
        return {'session_id':identifier,'observation_id':session.observation,'page':public_page(session.page.url),
            'bookmark_url':bookmark_url(session.page.url),
            'title':title,'fields':fields[:24],'actions':actions[:24],'captcha':captcha,
            'blocked_origins':sorted(session.blocked)[:12],'network_blocked':session.network_blocked,
            'steps_remaining':max(0,self.max_steps-session.steps),
            'public_only':True,'purchase_validated':False,'submitted':False}

    async def act(self,owner,identifier,observation_id,reference):
        session=await self.get(owner,identifier)
        async with session.lock:
            if observation_id!=session.observation or reference not in session.actions:
                raise BrowserError('browser_stale','La página cambió. Revisa el estado antes de continuar.')
            if session.network_blocked:
                raise BrowserError('browser_intervention','Se bloqueó una solicitud de la página. Requiere intervención.')
            if session.steps>=self.max_steps:
                raise BrowserError('browser_steps','La revisión alcanzó su límite de navegación. Requiere revisión humana.')
            frame,item=session.actions[reference]
            if item['kind']=='link' and origin(item['url']) not in session.allowed:
                raise BrowserError('browser_origin','Autoriza el nuevo dominio antes de abrirlo.',403)
            session.actions.pop(reference)
            session.observation=''
            session.steps+=1  # Consumir ANTES del clic: no se reintenta por fallos.
            if item['kind']=='link':
                await session.page.goto(https_url(item['url']),wait_until='domcontentloaded',timeout=8000)
            else:
                locator=frame.locator('[data-public-nav-ref="'+item['selector']+'"]')
                if await locator.count()!=1:
                    raise BrowserError('browser_stale','El control cambió. Revisa la página nuevamente.')
                await locator.click(timeout=4000)
            return await self._observe(identifier,session)

    async def allow(self,owner,identifier,allowed):
        session=await self.get(owner,identifier)
        async with session.lock:
            allowed={origin(value) for value in allowed}
            if not allowed or len(allowed)>12 or not session.allowed<=allowed:
                raise BrowserError('browser_origin','Permisos de revisión no válidos.',403)
            session.allowed=allowed
            if session.blocked and session.blocked<=allowed and not session.unsafe_blocked:
                session.blocked.clear()
                session.network_blocked=False
                # Recargar solo la página ya autorizada; nunca enviar formularios.
                await session.page.reload(wait_until='domcontentloaded',timeout=8000)
            return await self._observe(identifier,session)

    async def close_session(self,owner,identifier):
        session=await self.get(owner,identifier)
        async with session.lock:
            self.sessions.pop(identifier,None)
            await session.context.close()
        return {'closed':True}
