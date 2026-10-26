"""Límite de presentación del chat; nunca se aplica a revisiones humanas."""
import re


def brief_reply(text, limit=160):
    text = re.sub(r'\s+', ' ', str(text)).strip()
    # Retira ofertas genéricas; las preguntas por un dato concreto se conservan.
    text = re.sub(r'(?:[¿¡]?\s*(?:quieres que te (?:ayude|guíe|oriente)|'
        r'quieres (?:ayuda|instrucciones paso a paso)|te (?:ayudo|guío)|'
        r'puedo ayudarte en algo)[^?]*\?)\s*$', '', text, flags=re.I).strip()
    text = re.sub(r'^(?:claro|por supuesto|con gusto|perfecto)[,!:. ]+', '', text, flags=re.I)
    if not text:
        return 'Escribe la tarea que necesitas.'
    if len(text) <= limit:
        return text
    # Prefiere frases completas. No corta números decimales ni una revisión.
    boundaries = list(re.finditer(r'[.!?](?:\s|$)', text[:limit + 1]))
    if boundaries:
        return text[:boundaries[-1].start() + 1]
    return text[:limit - 1].rsplit(' ', 1)[0].rstrip(' ,;:') + '…'
