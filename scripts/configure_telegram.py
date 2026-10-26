"""Valida por defecto. --apply registra el webhook del bot; nunca imprime su token."""
import argparse
import asyncio
import base64
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.app.billing.telegram import TelegramClient
from backend.app.config import Settings
from backend.app.errors import AppError


async def configure(settings,url,*,apply=False,client=None):
    origin=urlsplit(url)
    if origin.scheme!='https' or origin.hostname not in settings.hosts or origin.path not in {'','/'} or origin.query or origin.fragment or origin.username or origin.password:
        raise AppError(422,'telegram_url','Usa el origen HTTPS público incluido en APP_ALLOWED_HOSTS.')
    secret=settings.telegram_webhook_secret.get_secret_value()
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}',secret):
        raise AppError(422,'telegram_secret','TELEGRAM_WEBHOOK_SECRET debe tener de 32 a 256 caracteres alfanuméricos, guiones o guiones bajos.')
    if not settings.telegram_bot_token.get_secret_value() or not settings.telegram_allowed_user_ids:
        raise AppError(422,'telegram_configuration','Faltan el token del bot o los usuarios autorizados.')
    users=settings.telegram_allowed_user_ids.split(',')
    if not 1<=len(users)<=100 or any(not re.fullmatch(r'[1-9][0-9]{0,15}',value.strip()) or int(value.strip())>=2**52 for value in users):
        raise AppError(422,'telegram_users','La lista debe contener únicamente IDs numéricos positivos de cuentas autorizadas.')
    if len(base64.b64decode(settings.billing_encryption_key.get_secret_value(),altchars=b'-_',validate=True))!=32:
        raise AppError(422,'billing_key','Configura una clave de cifrado válida antes de activar Telegram.')
    if not apply:
        print('Configuración comprobada sin llamadas externas. Usa --apply únicamente cuando autorices registrar el webhook.')
        return
    client=client or TelegramClient(settings)
    target=url.rstrip('/')+'/integrations/telegram/webhook'
    try:
        previous=await client._call('getWebhookInfo',json={})
        if not isinstance(previous,dict) or previous.get('url') not in {'',target}:
            raise AppError(409,'telegram_webhook_exists','El bot ya está conectado a otra dirección. No se ha sustituido ese webhook.')
        accepted=await client._call('setWebhook',json={'url':target,
            'secret_token':secret,'allowed_updates':['message','callback_query'],'drop_pending_updates':False,'max_connections':4})
        if accepted is not True:
            raise AppError(502,'telegram_webhook','Telegram no confirmó el registro. No se reintentó.')
        current=await client._call('getWebhookInfo',json={})
        if not isinstance(current,dict) or current.get('url')!=target:
            raise AppError(502,'telegram_webhook','No se confirmó la dirección registrada. Revisa la configuración antes de repetir.')
        print('Webhook registrado y dirección comprobada. Prueba /start desde la cuenta vinculada; este comando no comprueba todavía una respuesta del agente.')
        print('No se enviaron correos ni se hicieron llamadas de IA durante el registro.')
    finally:
        await client.close()


async def run():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True,help='Origen HTTPS público del backend, sin rutas ni credenciales')
    parser.add_argument('--apply',action='store_true',help='Autoriza registrar el webhook de Telegram')
    args=parser.parse_args()
    await configure(Settings(),args.url,apply=args.apply)


if __name__=='__main__':
    try:
        asyncio.run(run())
    except (ValueError,TypeError):
        raise SystemExit('Revisa la configuración de cifrado; no se ha mostrado ningún valor secreto.') from None
    except AppError as error:
        raise SystemExit(error.message) from None
