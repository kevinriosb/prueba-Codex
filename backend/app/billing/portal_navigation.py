"""Pasos públicos inequívocos; las decisiones ambiguas quedan para el agente/persona."""
import re

from .portal_memory import brand_options,brand_words,has_billing_form,matching_brand,same_brand_label


def action_filter(ticket,snapshot,action):
    """Explica los filtros existentes; no añade permisos ni opciones nuevas."""
    text=action['label'].casefold()
    label=' '.join(brand_words(action['label']))
    if re.search(r'login|iniciar sesi[oó]n|registro|privacidad|aviso|descarg|emitir|generar|enviar|solicitar|cancelar',text):return 'label_policy'
    if action.get('brand') and not same_brand_label(ticket.get('merchant'),action['label']):return 'brand_mismatch'
    if re.search(r'facturaci[oó]n|factura electr[oó]nica|invoic|billing|cfdi',text):return None
    if not brand_options(snapshot) and action['kind']=='expand' and label in {'menu','menú','navegacion','navegación'}:return None
    matched=matching_brand(ticket,snapshot)
    if matched and matched['reference']==action['reference']:return None
    return 'not_billing_hint'


def next_public_action(ticket,snapshot):
    if snapshot['captcha'] or snapshot.get('login_required') or snapshot.get('unsupported_required') or snapshot['network_blocked'] or not snapshot['steps_remaining'] or has_billing_form(snapshot):
        return None
    matched=matching_brand(ticket,snapshot)
    if snapshot.get('brand_selection'):
        # En una selección explícita manda la marca del ticket; un enlace
        # genérico del pie no debe sacar al agente aunque no se hayan podido
        # leer las opciones. Su ausencia no convierte el pie en la ruta correcta.
        return matched
    choices=[]
    for action in snapshot['actions']:
        label=' '.join(brand_words(action['label']))
        # brand_words quita «facturación»: comparar también el texto original.
        text=action['label'].casefold()
        if re.search(r'login|iniciar sesi[oó]n|registro|privacidad|aviso|descarg|emitir|generar|enviar|solicitar|cancelar',text):
            continue
        if action.get('brand') and not same_brand_label(ticket.get('merchant'),action['label']):
            continue
        if re.search(r'facturaci[oó]n|factura electr[oó]nica|invoic|billing|cfdi',text):
            choices.append(action)
        elif not brand_options(snapshot) and action['kind']=='expand' and label in {'menu','menú','navegacion','navegación'}:
            choices.append(action)
    if len(choices)==1:
        return choices[0]
    if choices:
        return None
    # Nunca escoger otra marca ni recorrer todas las opciones por ensayo.
    return matched
