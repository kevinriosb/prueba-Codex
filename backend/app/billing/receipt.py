"""Normalización de datos visibles y QR local; no visita enlaces del ticket."""
import base64
import io
import ipaddress
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit

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
    return [field for field in ('merchant', 'purchase_date', 'folio', 'total') if not ticket.get(field)]
