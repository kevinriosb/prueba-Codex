"""Normalización de datos visibles y QR local; no visita enlaces del ticket."""
import base64
import io
import ipaddress
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit,unquote

from PIL import Image
import zxingcpp


def portal_url(value):
    if not isinstance(value, str) or len(value) > 500:
        return None
    value = value.strip()
    if not value or any(char.isspace() or ord(char) < 32 for char in value) or '\\' in value:
        return None
    if '://' not in value:
        value = 'https://' + value
    try:
        uri = urlsplit(value)
        host = (uri.hostname or '').encode('idna').decode().lower()
        if uri.scheme not in {'http', 'https'} or uri.username or uri.password or not host:
            return None
        if uri.port not in {None, 80, 443}:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if '.' not in host or host.endswith(('.local', '.localhost', '.internal')):
                return None
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
                return None
            if host.rsplit('.',1)[-1].isdigit():
                return None
        return urlunsplit((uri.scheme, uri.netloc, uri.path, uri.query, uri.fragment))
    except (ValueError, UnicodeError):
        return None


def purchase_date(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def unrelated_billing_link(url,evidence=''):
    """Una encuesta o promoción no se vuelve fiscal por contener un QR."""
    address=unquote(str(url or '')).casefold()
    survey_url=re.search(r'(?:^|[/.?&=_-])(?:encuestas?|surveys?|feedback|opinion|satisfaccion|sorteo)(?:$|[/.?&=_-])',address)
    context=re.search(r'\b(?:encuestas?|surveys?|tu opini[oó]n|cu[eé]ntanos|califica|satisfacci[oó]n|sorteo|giveaway)\b',str(evidence or '').casefold())
    return bool(survey_url or context)


def purchase_total(value):
    if value is None:
        return None
    text = str(value).strip()
    text = re.sub(r'^(?:MXN\s*|\$\s*)', '', text, flags=re.I)
    text = re.sub(r'\s*MXN$', '', text, flags=re.I).strip()
    if re.fullmatch(r'\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?', text):
        text = text.replace(',', '')
    if not re.fullmatch(r'\d{1,10}(?:\.\d{1,2})?', text):
        return None
    try:
        return text if Decimal(text) > 0 else None
    except InvalidOperation:
        return None


def receipt_qr_urls(document):
    if document['mime'] != 'image/jpeg':
        return []
    try:
        with Image.open(io.BytesIO(base64.b64decode(document['data']))) as photo:
            decoded = zxingcpp.read_barcodes(photo, formats=zxingcpp.BarcodeFormat.QRCode)
        urls = [portal_url(code.text) for code in decoded[:5]]
        return list(dict.fromkeys(url for url in urls if url))
    except Exception:
        return []  # Un QR ausente o deteriorado no invalida los demás datos.


def missing_receipt_fields(ticket):
    return [field for field in ('merchant', 'purchase_date', 'folio', 'total')
        if not ticket.get(field) and (field!='folio' or not any(ticket.get(key) for key in ('account_number','reference_number','billing_code')))]


def purchase_reference(ticket):
    for key,label in (('folio','Folio'),('account_number','Cuenta'),('reference_number','Referencia'),('billing_code','Código')):
        if ticket.get(key):
            return key,label,ticket[key]
    return None,'Referencia',None


def pending_receipt_fields(ticket):
    fields=missing_receipt_fields(ticket)
    portal_fields=list((ticket.get('portal_inspection') or {}).get('fields',[]))
    portal_fields+= [item for item in (ticket.get('portal_browser') or {}).get('fields',[]) if item.get('required')]
    identifiers={item['key'] for item in portal_fields} & {'account_number','reference_number','billing_code','folio'}
    if identifiers and 'folio' not in identifiers and 'folio' in fields:
        fields.remove('folio')
    for item in portal_fields:
        if item['key'] in {'account_number','reference_number','billing_code','folio','web_id','store_number','toll_station','lane','purchase_time'} and not ticket.get(item['key']) and item['key'] not in fields:
            fields.append(item['key'])
    return fields


def browser_missing_fields(ticket):
    """Campos visibles sin valor; pueden ser alternativas, no requisitos universales."""
    allowed={'account_number','folio','reference_number','billing_code','web_id','store_number','toll_station','lane','purchase_time','purchase_date','total'}
    return list(dict.fromkeys(item['key'] for item in (ticket.get('portal_browser') or {}).get('fields',[])
        if item.get('key') in allowed and not ticket.get(item['key'])))
