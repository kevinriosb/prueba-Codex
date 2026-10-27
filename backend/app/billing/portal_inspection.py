"""Consulta pública de formularios aprobados. Nunca envía datos ni pulsa botones."""
import asyncio
import copy
import ipaddress
import re
import socket
import time
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx2


LABELS = {'account_number':'Cuenta', 'folio':'Folio', 'reference_number':'Referencia',
    'billing_code':'Código de facturación', 'purchase_date':'Fecha', 'total':'Importe',
    'rfc':'RFC', 'postal_code':'CP fiscal', 'email':'Correo', 'fiscal_regime':'Régimen', 'cfdi_use':'Uso CFDI',
    'web_id':'Web ID','toll_station':'Caseta','lane':'Carril','purchase_time':'Hora'}
IDENTIFIERS = ('account_number', 'folio', 'reference_number', 'billing_code')


def field_key(text):
    text = ''.join(char for char in unicodedata.normalize('NFD', text.casefold())
        if unicodedata.category(char) != 'Mn')
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    patterns = {'web_id':r'\b(web\s*id|webid)\b', 'toll_station':r'\b(caseta|plaza de cobro)\b',
        'lane':r'\b(carril|lane)\b','purchase_time':r'\b(hora|time)\b',
        'account_number':r'\b(cuenta|account|accountnumber|consumptionaccount)\b',
        'billing_code':r'\b(billing code|codigo de facturacion|codigofacturacion)\b',
        'folio':r'\b(folio|ticket|ticketnumber)\b', 'reference_number':r'\b(referencia|reference)\b',
        'rfc':r'\brfc\b', 'postal_code':r'\b(cp|codigo postal|postal code|zip)\b',
        'cfdi_use':r'\b(uso cfdi|uso de cfdi|usocfdi)\b', 'fiscal_regime':r'\b(regimen|regime)\b',
        'email':r'\b(email|correo)\b', 'purchase_date':r'\b(fecha|date)\b', 'total':r'\b(total|importe|amount)\b'}
    return next((key for key, pattern in patterns.items() if re.search(pattern, text)), None)


class FormFields(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.labels, self.controls = {}, []
        self.current_label = None
        self.last_label = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'label':
            self.current_label = {'for':attrs.get('for'), 'text':''}
            self.last_label = ''
        if tag in {'input','select','textarea'} and len(self.controls) < 40:
            if attrs.get('type','text').casefold() in {'hidden','password','file','submit','button','reset','checkbox','radio'} or 'disabled' in attrs:
                return
            self.controls.append({'attrs':attrs,'label':self.last_label,'enclosing_label':self.current_label})
            if self.current_label is not None:
                self.current_label['used'] = True
            self.last_label = ''

    def handle_data(self, data):
        if self.current_label is not None:
            self.current_label['text'] = (self.current_label['text'] + data)[:120]

    def handle_endtag(self, tag):
        if tag == 'label' and self.current_label is not None:
            label = self.current_label
            self.last_label = '' if label.get('used') else label['text']
            if label['for']:
                self.labels[label['for']] = label['text']
            self.current_label = None

    def fields(self):
        fields = []
        for control in self.controls:
            attrs = control['attrs']
            visible = self.labels.get(attrs.get('id')) or (control['enclosing_label'] or {}).get('text') or control['label']
            hints = ' '.join([attrs.get('name',''),attrs.get('id',''),attrs.get('placeholder',''),attrs.get('aria-label','')])
            key = field_key(visible or '') or field_key(hints)
            if key and key not in {item['key'] for item in fields}:
                fields.append({'key':key,'label':LABELS[key]})
        return fields


def canonical_page(url):
    uri = urlsplit(url)
    if uri.scheme != 'https' or not uri.hostname or uri.username or uri.password or uri.port not in {None,443}:
        raise ValueError('Solo páginas HTTPS públicas.')
    return urlunsplit(('https',uri.hostname.lower(),uri.path.rstrip('/') or '/', '', ''))


async def public_addresses(hostname):
    records = await asyncio.to_thread(socket.getaddrinfo, hostname,443,0,socket.SOCK_STREAM)
    return list(dict.fromkeys(item[4][0] for item in records))


class PortalInspector:
    def __init__(self, settings, *, client=None, resolver=public_addresses):
        self.settings, self.client, self.resolver = settings, client, resolver
        urls = [part.strip() for part in settings.billing_portal_inspection_urls.split(',') if part.strip()]
        self.allowed = {canonical_page(url) for url in urls}
        self.cache = {}
        self.lock = asyncio.Lock()

    async def close(self):
        if self.client:
            await self.client.aclose()

    async def _read(self, client, page):
        uri = urlsplit(page)
        addresses = await self.resolver(uri.hostname)
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError('Destino no público.')
        addresses=sorted(addresses,key=lambda address:':' in address)
        address = addresses[0]
        authority = '['+address+']' if ':' in address else address
        pinned = urlunsplit(('https',authority,uri.path,'',''))
        # La conexión usa la IP validada y conserva Host/SNI para verificar TLS.
        async with client.stream('GET',pinned,headers={'Host':uri.hostname,'Accept':'text/html'},
                extensions={'sni_hostname':uri.hostname},follow_redirects=False) as response:
            if response.status_code in {301,302,303,307,308}:
                target = urljoin(page,response.headers.get('location',''))
                if canonical_page(target) not in self.allowed:
                    raise ValueError('Redirección fuera de las páginas aprobadas.')
                target_uri=urlsplit(target)
                redirected=urlunsplit(('https',target_uri.hostname,target_uri.path,'',''))
                return None,redirected
            if response.status_code != 200 or 'text/html' not in response.headers.get('content-type','').lower():
                raise ValueError('No se recibió un formulario HTML.')
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 262_144:
                    raise ValueError('Página demasiado grande.')
                chunks.append(chunk)
            return b''.join(chunks).decode('utf-8','replace'),None

    async def inspect(self, url):
        try:
            page = canonical_page(url)
        except (ValueError,TypeError):
            return {'status':'unsupported','fields':[],'source':'none','lookup_performed':False}
        if page not in self.allowed:
            return {'status':'unsupported','fields':[],'source':'none','lookup_performed':False}
        async with self.lock:
            cached = self.cache.get(page)
            if cached and cached['expires'] > time.time():
                return copy.deepcopy(cached['result'])
            result = {'status':'unavailable','fields':[],'source':'none','lookup_performed':False}
            try:
                async with asyncio.timeout(self.settings.billing_portal_inspection_timeout_seconds):
                    async def read(client):
                        content, redirected = await self._read(client,page)
                        if redirected:
                            content, redirected = await self._read(client,redirected)
                        if content is None or redirected:
                            raise ValueError('No se pudo resolver el formulario.')
                        parser = FormFields()
                        parser.feed(content)
                        fields = parser.fields()
                        return {'status':'verified' if fields else 'unclear','fields':fields,
                            'source':'public_html','lookup_performed':False}
                    if self.client:
                        result = await read(self.client)
                    else:
                        async with httpx2.AsyncClient(trust_env=False,follow_redirects=False,timeout=5) as client:
                            result = await read(client)
            except Exception:
                pass  # No se registran página, cookies ni excepciones del comercio.
            if not result['fields'] and page == 'https://f.zetus.mx/albat':
                # Evidencia de la captura aportada: no se hace pasar por una lectura en vivo.
                result.update(status='template',fields=[{'key':'account_number','label':'Cuenta'}],
                    source='user_screenshot_template')
            result['checked_at'] = time.time()
            self.cache[page] = {'expires':time.time()+(3600 if result['status']=='verified' else 60),'result':result}
            return copy.deepcopy(result)


def portal_plan(ticket, inspection):
    fields = []
    for item in inspection.get('fields',[]):
        key = item['key']
        fields.append({'key':key,'label':item['label'],'value':ticket.get(key),
            'matched':bool(ticket.get(key))})
    alternatives = [{'key':key,'label':LABELS[key],'value':ticket[key]}
        for key in IDENTIFIERS if ticket.get(key) and key not in {item['key'] for item in fields}]
    proposed=next((item for key in ('folio','reference_number','billing_code','account_number')
        for item in alternatives if item['key']==key),None)
    return dict(inspection, fields=fields, alternatives=alternatives,proposed_reference=proposed,
        purchase_validated=False, submitted=False)
