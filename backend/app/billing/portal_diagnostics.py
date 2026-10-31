"""Metadatos de navegación pública; nunca valores de controles ni datos fiscales."""
import re
import unicodedata
from urllib.parse import unquote, urlsplit

STOP_REASONS=frozenset({'form_found','step_limit','time_limit','no_actionable_element',
    'action_filtered','permission_required','captcha_or_unfillable','error'})
FILTERS=frozenset({'label_policy','brand_mismatch','not_billing_hint','ambiguous_actions',
    'brand_selection','cycle','login_required','unsupported_required','captcha','submit_control',
    'form_control','missing_target','already_expanded','tracking_url','same_page','model_stopped',
    'stale_reference','private_session','partial_view','invalid_target','model_budget'})
ROLES=frozenset({'link','button','option','tab','summary','textbox','combobox','unknown'})
STEP_FIELDS=frozenset({'step','action','role','element_text','host','path','fields_before','fields_after',
    'reason','filter','elapsed_ms','duration_ms'})
PRIVATE_PATTERN=re.compile(r'[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}|[^\s@]+@[^\s@]+|\d|[?#]|https?://',re.I)
PRIVATE_KEYS={'rfc','issuer_rfc','name','email','postal_code','total','folio','account_number',
    'reference_number','billing_code','web_id','store_number','register_number','purchase_date',
    'purchase_time','merchant','phone','telephone'}


def normalized(value):
    return unicodedata.normalize('NFKC',str(value)).casefold()


def private_values(*records):
    values=[]
    for record in records:
        for key,value in record.items():
            if key not in PRIVATE_KEYS or value is None:continue
            text=str(value).strip()
            if len(text)>=3:values.append(text)
            if key in {'name','merchant'}:
                values.extend(word for word in re.findall(r'\w+',text) if len(word)>=3)
    return values


def safe_text(value,private=(),*,limit=80):
    text=' '.join(unquote(str(value or '')).split())
    if any(normalized(item) in normalized(text) for item in private if item):return '[omitido]'
    text=re.sub(r'https?://\S+|\?\S*|#\S*','[omitido]',text,flags=re.I)
    text=PRIVATE_PATTERN.sub('[omitido]',text)
    return text[:limit]


def validate_address(event,private=()):
    if set(event)!={'address_purpose','address_result','host','path'}:raise ValueError('Campos de dirección no admitidos')
    if event['address_purpose'] not in {'billing','official','other','unclear'}:raise ValueError('Propósito no admitido')
    if event['address_result'] not in {'valid','no_response','blocked','survey_filtered','not_billing','not_literal','not_checked'}:raise ValueError('Resultado no admitido')
    for key in ('host','path'):
        value=event[key]
        if not isinstance(value,str) or len(value)>240 or PRIVATE_PATTERN.search(value) or any(normalized(v) in normalized(value) for v in private if v):
            raise ValueError('Dato privado en diagnóstico de dirección')
    return event


def validate_step(event,private=()):
    """Rechaza contaminación, incluso si un llamador intenta omitir el saneado."""
    if set(event)!=STEP_FIELDS:raise ValueError('Campos de diagnóstico no admitidos')
    if event['reason'] is not None and event['reason'] not in STOP_REASONS:raise ValueError('Razón no admitida')
    if event['filter'] is not None and event['filter'] not in FILTERS:raise ValueError('Filtro no admitido')
    if event['action'] not in {'click','write','wait'} or event['role'] not in ROLES:raise ValueError('Acción no admitida')
    for key in ('step','fields_before','fields_after','elapsed_ms','duration_ms'):
        if type(event[key]) is not int or event[key]<0:raise ValueError('Contador no válido')
    for key in ('element_text','host','path'):
        value=event[key]
        if not isinstance(value,str) or len(value)>(80 if key=='element_text' else 240):raise ValueError('Texto no válido')
        if PRIVATE_PATTERN.search(value) or any(normalized(item) in normalized(value) for item in private if item):
            raise ValueError('Dato privado en diagnóstico')
    return event


def step_event(snapshot,*,before=None,element=None,action='wait',step=None,reason=None,filter=None,
        elapsed_ms=0,duration_ms=0,private=()):
    element=element or {}
    try:
        address=urlsplit(snapshot.get('page',''))
        host=safe_text(address.hostname or '',private,limit=240)
        path=safe_text(address.path or '/',private,limit=240)
    except ValueError:host,path='',''
    role=element.get('role','unknown')
    event={'step':max(0,step if step is not None else (snapshot.get('navigation_step') or 1)),
        'action':action,'role':role if role in ROLES else 'unknown',
        'element_text':safe_text(element.get('diagnostic_label') or element.get('label') or 'sin_nombre',private),
        'host':host,'path':path,'fields_before':len((before or {}).get('fields',[])),
        'fields_after':len(snapshot.get('fields',[])),'reason':reason,'filter':filter,
        'elapsed_ms':max(0,int(elapsed_ms)),'duration_ms':max(0,int(duration_ms))}
    return validate_step(event,private)


def stop_detail(ticket,snapshot,*,time_limit=False,step_limit=False):
    if snapshot.get('captcha'):return 'captcha_or_unfillable','captcha'
    if snapshot.get('login_required'):return 'captcha_or_unfillable','login_required'
    if snapshot.get('unsupported_required'):return 'captcha_or_unfillable','unsupported_required'
    if snapshot.get('public_only') is False:return 'captcha_or_unfillable','private_session'
    if snapshot.get('network_blocked'):
        return ('permission_required',None) if snapshot.get('block_reason')=='permission' else ('error',None)
    from .portal_memory import public_form_ready
    if public_form_ready(snapshot):return 'form_found',None
    if time_limit:return 'time_limit',None
    if step_limit or not snapshot.get('steps_remaining',1):return 'step_limit',None
    from .portal_navigation import action_filter, next_public_action
    if not snapshot.get('actions'):
        filtered=snapshot.get('filtered_actions') or []
        return ('action_filtered',filtered[0]['filter']) if filtered else ('no_actionable_element',None)
    filters=[action_filter(ticket,snapshot,a) for a in snapshot['actions']]
    if all(filters):return 'action_filtered',filters[0]
    if next_public_action(ticket,snapshot) is None:return 'action_filtered','ambiguous_actions'
    return 'action_filtered','model_stopped'


def stop_message(reason,step,filter=None):
    if reason=='time_limit':return f'Me detuve en el paso {step} de la ruta: se agotó el tiempo.'
    if reason=='step_limit':return f'Me detuve en el paso {step} de la ruta: alcancé el límite de pasos.'
    if reason=='permission_required':return 'Me detuve porque falta tu permiso para abrir otro dominio.'
    if reason=='captcha_or_unfillable':
        if filter=='login_required':return 'La pantalla solicita iniciar sesión. Necesito tu ayuda; no intenté entrar ni saltarla.'
        return 'Encontré una verificación o un campo que no puedo completar. Necesito tu ayuda; no intenté resolverlo ni saltarlo.'
    if reason=='error':return 'Me detuve por un error técnico al revisar esta pantalla.'
    if reason=='action_filtered':return 'Los controles de esta pantalla quedaron fuera de la selección de navegación. Necesito tu ayuda para continuar.'
    return 'No encontré un botón para avanzar en esta pantalla.'
