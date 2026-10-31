"""Calidad declarada por la lectura; nunca completa texto ilegible ni valores fiscales."""
from decimal import Decimal
from urllib.parse import urlsplit,urlunsplit
from .receipt import purchase_total

LABELS={'merchant':'comercio','store_number':'tienda','purchase_date':'fecha','purchase_time':'hora',
    'folio':'folio','total':'total','billing_url':'dirección de facturación','subtotal':'subtotal',
    'tax':'IVA','tip':'propina','total_without_tip':'total sin propina','total_with_tip':'total con propina'}


def assess(data):
    result=dict(data)
    for key in ('subtotal','tax','tip','total_without_tip','total_with_tip'):
        value=data.get(key)
        result[key]='0.00' if value is not None and str(value).strip() in {'0','0.00','0.0'} else purchase_total(value)
    confidence={item['key']:item['confidence'] for item in data.get('field_confidence') or []}
    if all(result.get(k) is not None for k in ('subtotal','tax','total')):
        result['amounts_consistent']=abs(Decimal(result['subtotal'])+Decimal(result['tax'])-Decimal(result['total']))<=Decimal('0.02')
        if not result['amounts_consistent']:confidence['total']='low'
    else:result['amounts_consistent']=None
    result['field_confidence']=[{'key':k,'confidence':v} for k,v in confidence.items()]
    return result


def warnings(ticket):
    messages=[]
    zones={'bottom':'a la parte de abajo','top':'a la parte de arriba','middle':'a la parte central','whole':'al ticket completo'}
    subjects={'billing_address':'la dirección de facturación','qr':'el QR','purchase_fields':'los datos de compra'}
    for item in ticket.get('photo_issues') or []:
        verb='están' if item['subject']=='purchase_fields' else 'está'
        messages.append('Acércate '+zones[item['zone']]+', donde '+verb+' '+subjects[item['subject']]+': no se alcanza a leer con claridad.')
    if ticket.get('amounts_consistent') is False:messages.append('Revisar importes: subtotal + IVA no coincide con el total; no corregí ni deduje ningún valor.')
    low=[LABELS[i['key']] for i in ticket.get('field_confidence') or [] if i['confidence'] in {'low','unreadable'} and not confirmed(ticket,i['key'])]
    if low:messages.append('Confirma los campos de baja confianza: '+', '.join(low)+'.')
    if ticket.get('qr_text_mismatch'):messages.append('El QR y el texto impreso difieren. Se propone el QR decodificado; revisa el dominio antes de autorizar.')
    return list(dict.fromkeys(messages))


def public_address(value):
    """Vista para el dueño: conserva escritura literal excepto parámetros/fragmento."""
    text=str(value or '').strip()
    try:
        parsed=urlsplit(text if '://' in text else '//'+text)
        if parsed.username or parsed.password:return '[dirección omitida]'
        return urlunsplit(('',parsed.netloc,parsed.path,'','')).removeprefix('//')[:500] or '[sin dirección legible]'
    except ValueError:return '[dirección no válida]'


def display_address(value):
    text=str(value or '').strip()
    scheme='https://' if text.startswith('https://') else 'http://' if text.startswith('http://') else ''
    return scheme+public_address(text)


def url_notice(ticket):
    literal=ticket.get('billing_url_literal') or ticket.get('portal_printed_url_candidate')
    if not literal:return ''
    target=(ticket.get('portal_entry_check') or {}).get('url') or ticket.get('billing_url') or ticket.get('portal_printed_url_candidate') or literal
    text='Lectura literal (sin parámetros): '+display_address(literal)+'\nDominio y ruta propuestos: '+display_address(target)
    if ticket.get('qr_text_mismatch'):text+='\nAviso: el texto y el QR difieren; tiene prioridad el QR decodificado.'
    return text


def field_total(ticket,field):
    import re
    key=field.get('key')
    if not confirmed(ticket,key) and any(i['key']==key and i['confidence'] in {'low','unreadable'} for i in ticket.get('field_confidence') or []):return None
    if field.get('key')=='total' and re.search(r'\bsin\s+propina\b',field.get('label',''),re.I):
        return ticket.get('total_without_tip')  # Si no está explícito, se pide; no se resta.
    return ticket.get(field.get('key'))


def confirmed(ticket,key):
    values=ticket.get('confidence_confirmed') or {}
    return isinstance(values,dict) and key in values and values[key]==ticket.get(key)


def match_photo(data,tickets):
    """Identidad parcial con conflictos conservadores; nunca usa solo comercio/total."""
    import unicodedata
    def norm(value):return ' '.join(unicodedata.normalize('NFKC',str(value)).casefold().split())
    clear=[];possible=[]
    keys=('merchant','store_number','purchase_date','purchase_time','folio','total')
    for ticket in tickets:
        equal={k for k in keys if data.get(k) and ticket.get(k) and norm(data[k])==norm(ticket[k])}
        different={k for k in keys if data.get(k) and ticket.get(k) and norm(data[k])!=norm(ticket[k])}
        uncertain={i['key'] for record in (data,ticket) for i in record.get('field_confidence') or [] if i['confidence'] in {'low','unreadable'}}
        reliable=equal-uncertain
        # Folio/importe pueden ser ambiguos entre fotos: el conflicto exige preguntar.
        if len(reliable)>=3 and 'merchant' in reliable and ('folio' in reliable or {'purchase_date','purchase_time','total'}<=reliable) and not different:
            clear.append(ticket)
        elif equal and not different & {'merchant','store_number','purchase_date'}:possible.append(ticket)
    return (clear[0] if len(clear)==1 else None),list({t['id']:t for t in clear+possible}.values())


def read_addresses(data,document):
    import re
    from .receipt import receipt_qr_urls,portal_url,unrelated_billing_link
    data=dict(data)
    qr_urls=receipt_qr_urls(document)
    literal=data.get('billing_url')
    if literal:
        if data.get('printed_addresses') is None:
            data['legacy_printed_address']={'url':literal,'purpose':'billing','evidence':data.get('billing_url_evidence') or ''}
        data['billing_url_literal']=next((i['url'] for i in data.get('printed_addresses') or [] if i.get('url') and portal_url(i['url'])==literal),literal)
    printed=data.get('printed_addresses') or []
    qr_context=[i for i in printed if not i.get('url')]
    billing=(any(i.get('purpose')=='billing' for i in qr_context) if qr_context else
        any(i.get('purpose')=='billing' for i in printed) or bool(re.search(r'factur|cfdi',data.get('billing_url_evidence') or '',re.I)))
    if literal and unrelated_billing_link(literal,data.get('billing_url_evidence')):data['billing_url']=None
    if len(qr_urls)==1 and billing and not unrelated_billing_link(qr_urls[0],data.get('billing_url_evidence')):
        data.update(qr_text_mismatch=bool(literal and portal_url(literal)!=qr_urls[0]),
            billing_url_literal=data.get('billing_url_literal') or qr_urls[0],qr_billing_url=qr_urls[0],billing_url=qr_urls[0])
    return data,qr_urls
