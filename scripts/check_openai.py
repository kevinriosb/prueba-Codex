"""Diagnóstico de OpenAI sin generación, dependencias ni salida de secretos.

Se puede ejecutar por stdin dentro del contenedor para usar su entorno efectivo.
Solo envía cuerpos vacíos y consulta metadatos; nunca envía prompts o documentos.
"""
import json
import os
import re
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


BASE_URL = 'https://api.openai.com/v1'
MAX_BODY = 65_536


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Nunca reenviar Authorization a un destino indicado por una respuesta.
        return None


def request_metadata(opener, path, token=None, *, method='POST'):
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = Request(BASE_URL + path, data=b'{}' if method == 'POST' else None,
                      headers=headers, method=method)
    try:
        response = opener.open(request, timeout=15)
    except HTTPError as error:
        response = error
    with response:
        status = response.code
        raw = response.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        return status, {}
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        body = {}
    return status, body if isinstance(body, dict) else {}


def report_rejection(status, body, emit):
    error = body.get('error')
    code = error.get('code') if isinstance(error, dict) else None
    if status == 401:
        reason = 'Credencial rechazada. Revisa AI_API_KEY dentro del contenedor.'
    elif status == 403:
        reason = 'Acceso denegado. Revisa permisos del proyecto y disponibilidad regional.'
    elif status == 404:
        reason = 'Modelo no encontrado o sin acceso para este proyecto.'
    elif status == 429 and code == 'insufficient_quota':
        reason = 'Cuota o saldo de la API insuficientes; revisa Billing en OpenAI.'
    elif status == 429:
        reason = 'El proveedor ha limitado las solicitudes. No se reintentó.'
    elif 300 <= status < 400:
        reason = 'Redirección rechazada; no se reenviaron credenciales.'
    elif status >= 500:
        reason = 'El proveedor o la conexión intermedia devolvió un error de servicio.'
    else:
        reason = 'Respuesta inesperada. No se ha validado la integración.'
    # No mostrar el cuerpo, mensaje del proveedor, identificadores ni cabeceras.
    emit('Resultado: HTTP ' + str(status) + '. ' + reason)


def check(environment, *, opener=None, emit=print):
    emit('Comprobación sin generación de IA ni consumo de tokens de inferencia.')
    emit('No se modifican archivos, datos ni la configuración del servidor.')
    token = environment.get('AI_API_KEY', '')
    if not token:
        emit('Configuración: falta AI_API_KEY en el entorno del contenedor.')
        return 1
    if (token != token.strip() or not token.isascii()
            or any(ord(character) < 33 or ord(character) == 127 for character in token)
            or token.startswith(('"', "'")) or token.endswith(('"', "'"))
            or token.lower().startswith(('bearer ', 'ai_api_key=', 'openai_api_key='))):
        emit('Configuración: AI_API_KEY tiene un formato incorrecto; usa solo la clave, sin comillas ni prefijos.')
        return 1
    model = environment.get('AI_MODEL', 'gpt-4.1-mini')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}', model) or model.lower().startswith('sk-'):
        emit('Configuración: AI_MODEL no contiene un identificador de modelo válido.')
        return 1
    if environment.get('OPENAI_BASE_URL'):
        emit('Configuración: existe OPENAI_BASE_URL. Esta prueba usa únicamente el destino oficial de OpenAI.')
    opener = opener or build_opener(NoRedirects())
    try:
        # Control sin credencial: una respuesta permisiva no prueba autenticación.
        status, _ = request_metadata(opener, '/responses')
        if status != 401:
            emit('Control sin credencial: HTTP ' + str(status) + '. No se envió la clave y no se puede validar la autenticación.')
            return 1
        # Sin modelo NI entrada: no puede generar texto o ejecutar herramientas.
        status, body = request_metadata(opener, '/responses', token)
        error = body.get('error')
        if not (status == 400 and isinstance(error, dict)
                and error.get('code') == 'missing_required_parameter'
                and error.get('param') == 'model'):
            report_rejection(status, body, emit)
            return 1
        emit('Autenticación: el endpoint Responses aceptó la credencial antes de rechazar el cuerpo vacío (HTTP 400, falta model).')
        status, body = request_metadata(opener, '/models/' + quote(model, safe=''), token, method='GET')
        if status != 200 or body.get('id') != model or body.get('object') != 'model':
            report_rejection(status, body, emit)
            if status == 403:
                emit('Una clave restringida puede permitir Responses sin permitir consultar modelos; este resultado no demuestra que generar esté bloqueado.')
            return 1
        emit('Metadatos del modelo: disponibles (HTTP 200).')
        emit('La prueba NO verifica saldo disponible, permisos de generación ni una respuesta real. No reintentes el chat todavía: comparte este resultado.')
        return 0
    except (URLError, OSError, TimeoutError) as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, ssl.SSLCertVerificationError):
            emit('Conexión: falló la validación del certificado TLS. No se desactivó su comprobación.')
        elif isinstance(reason, socket.gaierror):
            emit('Conexión: no se pudo resolver el nombre del servidor de OpenAI.')
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            emit('Conexión: se agotó el tiempo de espera. No se reintentó.')
        else:
            emit('Conexión: no se pudo completar la consulta HTTPS a OpenAI. No se reintentó.')
        return 1


def main():
    try:
        return check(os.environ)
    except Exception:
        # Una excepción de red puede incluir URLs, cabeceras o valores privados.
        print('Diagnóstico interrumpido. No se muestran detalles internos ni valores privados.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
