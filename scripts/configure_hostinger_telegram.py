"""Vincula una primera cuenta a un bot nuevo, sin registrar todavía el webhook.

Python 3.9+, sin dependencias. Guarda solo las tres variables de Telegram.
El token se introduce oculto; la cuenta se verifica con un código de un solo uso.
"""
import base64
import getpass
import json
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import warnings

if __package__:
    from .update_openai_key import read_private, write_updated
else:
    from update_openai_key import read_private, write_updated


ROOT = Path(__file__).resolve().parents[1]
KEYS = ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_WEBHOOK_SECRET', 'TELEGRAM_ALLOWED_USER_IDS')


def variable(original, key):
    lines = [line for line in original.splitlines(keepends=True)
             if re.match(rb'^[ \t]*(?:export[ \t]+)?' + key.encode() + rb'(?:[ \t]*=|[ \t]*(?:\r?\n|$))', line, re.I)]
    if len(lines) != 1 or not lines[0].startswith(key.encode() + b'='):
        raise ValueError('Revisa la línea ' + key + ': debe existir una sola vez, sin export ni espacios en el nombre.')
    return lines[0].partition(b'=')[2].rstrip(b'\r\n').decode('ascii')


def render(original, token, user_id, secret):
    if not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{30,128}', token):
        raise ValueError('El token no tiene el formato esperado de BotFather. Pega únicamente su valor completo.')
    if type(user_id) is not int or not 0 < user_id < 2**52:
        raise ValueError('La cuenta de Telegram no tiene un identificador válido.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', secret):
        raise ValueError('El secreto de webhook no tiene el formato esperado.')
    values = dict(zip(KEYS, (token, secret, str(user_id))))
    return render_variables(original, values)


def render_variables(original, values):
    if not values or any(key not in KEYS or not isinstance(value,str) or any(char in value for char in '\r\n\x00') for key,value in values.items()):
        raise ValueError('Variables de Telegram no válidas.')
    for key in values:
        variable(original, key)
    result = []
    for line in original.splitlines(keepends=True):
        key = line.partition(b'=')[0].decode('ascii', errors='ignore')
        ending = b'\r\n' if line.endswith(b'\r\n') else (b'\n' if line.endswith(b'\n') else b'')
        result.append(key.encode() + b'=' + values[key].encode() + ending if key in values else line)
    return b''.join(result)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BotAPI:
    def __init__(self, token, *, opener=None):
        if not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{30,128}', token):
            raise ValueError('El token no tiene el formato esperado de BotFather.')
        self.token = token
        self.opener = opener or build_opener(NoRedirects())

    def call(self, method, payload=None):
        # Esta etapa es solo de lectura; nunca envía mensajes ni cambia webhooks.
        if method not in {'getMe', 'getWebhookInfo', 'getUpdates'}:
            raise ValueError('Operación de configuración no permitida.')
        request = Request('https://api.telegram.org/bot' + self.token + '/' + method,
            data=json.dumps(payload or {}).encode(), headers={'Content-Type':'application/json'}, method='POST')
        try:
            with self.opener.open(request, timeout=15) as response:
                content = response.read(1_000_001)
                if response.code != 200 or len(content) > 1_000_000:
                    raise ValueError()
            body = json.loads(content)
            if not isinstance(body, dict) or body.get('ok') is not True:
                raise ValueError()
            return body.get('result')
        except HTTPError as error:
            error.close()
            if error.code in {401, 404}:
                raise ValueError('Telegram rechazó el token. No se ha guardado la configuración.') from None
            raise ValueError('Telegram no confirmó la consulta. No se muestran detalles privados ni se reintenta automáticamente.') from None
        except (URLError, OSError, ValueError, TypeError):
            raise ValueError('No se pudo consultar Telegram de forma segura. No se muestran URLs ni detalles privados.') from None


def paired_user(updates, command):
    matches = set()
    if not isinstance(updates, list):
        raise ValueError('Telegram devolvió una respuesta inesperada.')
    for update in updates:
        if not isinstance(update, dict):
            continue
        message = update.get('message')
        if not isinstance(message, dict) or message.get('text') != command:
            continue
        sender, chat = message.get('from'), message.get('chat')
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            continue
        identifier = sender.get('id')
        if (type(identifier) is int and 0 < identifier < 2**52 and sender.get('is_bot') is False
                and chat.get('type') == 'private' and chat.get('id') == identifier):
            matches.add(identifier)
    if len(matches) > 1:
        raise ValueError('El código apareció en varias cuentas. No se autorizó ninguna; inicia otra vinculación.')
    return next(iter(matches)) if matches else None


def main():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('Ejecuta este asistente directamente en Termius, sin tuberías ni redirecciones.')
    path = ROOT / '.env.hostinger'
    original, _ = read_private(path)
    if variable(original, 'TELEGRAM_BOT_TOKEN') or variable(original, 'TELEGRAM_ALLOWED_USER_IDS'):
        raise ValueError('Ya hay una configuración de Telegram. Se conserva; este asistente es solo para la primera conexión.')
    try:
        encryption_key = base64.b64decode(variable(original, 'BILLING_ENCRYPTION_KEY'), altchars=b'-_', validate=True)
        if len(encryption_key) != 32:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Se necesita conservar la clave de cifrado válida antes de conectar Telegram.') from None
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            token = getpass.getpass('Token del BOT NUEVO de BotFather (oculto; Enter cancela): ')
        except getpass.GetPassWarning:
            raise ValueError('Esta terminal no permite ocultar el token. No se solicitó una entrada visible.') from None
    if not token:
        raise ValueError('Cancelado. No se ha cambiado la configuración.')
    client = BotAPI(token)
    bot, webhook = client.call('getMe'), client.call('getWebhookInfo')
    if not isinstance(bot, dict) or bot.get('is_bot') is not True or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', bot.get('username','')):
        raise ValueError('No se pudo verificar la identidad del bot.')
    if not isinstance(webhook, dict) or webhook.get('url') != '':
        raise ValueError('Este bot ya tiene un webhook. Se conserva sin cambios; utiliza el bot nuevo de esta instalación.')
    print('Bot comprobado: @' + bot['username'])
    if input('Si es el bot nuevo que acabas de crear, escribe SI para vincular tu cuenta: ').strip().upper() != 'SI':
        raise ValueError('Cancelado. No se ha cambiado la configuración.')
    command = '/vincular_' + secrets.token_urlsafe(24)
    started = time.monotonic()
    print('Abre https://t.me/' + bot['username'] + ', pulsa Iniciar y envía este código en privado:')
    print(command)
    print('El bot todavía no responderá. Este código es temporal: no lo compartas ni envíes documentos.')
    user_id = None
    for _ in range(3):
        input('Después de enviar el código por Telegram, vuelve aquí y pulsa Enter para comprobar: ')
        if time.monotonic() - started > 600:
            raise ValueError('La vinculación caducó. Ejecuta de nuevo el asistente; no se guardó la configuración.')
        user_id = paired_user(client.call('getUpdates', {'limit':100, 'timeout':0}), command)
        if user_id:
            break
        print('Todavía no aparece el código exacto en un chat privado. Revisa el bot y vuelve a comprobar.')
    if not user_id:
        raise ValueError('No se verificó tu cuenta. No se ha cambiado la configuración.')
    # La clave fiscal existente nunca se regenera o sustituye.
    secret = secrets.token_urlsafe(32)
    backup = write_updated(path, render(original, token, user_id, secret), original, label='telegram')
    print('Cuenta verificada y autorizada. Se guardaron únicamente las tres variables de Telegram.')
    print('Copia privada anterior: ' + backup.name)
    print('OpenAI, contraseña, cifrado y demás variables conservados. Aún no se registró el webhook ni se reinició el servicio.')
    return 0


def cli():
    try:
        return main()
    except ValueError as error:
        print(str(error))
    except (EOFError, KeyboardInterrupt):
        print('\nVinculación interrumpida. No se muestran valores privados.')
    except Exception:
        print('No se completó la vinculación. No compartas el token, el archivo privado ni sus copias.')
    return 1


if __name__ == '__main__':
    raise SystemExit(cli())
