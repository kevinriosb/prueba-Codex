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


async def run():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True,help='Origen HTTPS público del backend, sin rutas ni credenciales')
    parser.add_argument('--apply',action='store_true',help='Autoriza registrar el webhook de Telegram')
    args=parser.parse_args()
    settings=Settings()
    origin=urlsplit(args.url)
    if origin.scheme!='https' or origin.hostname not in settings.hosts or origin.path not in {'','/'} or origin.query or origin.fragment or origin.username or origin.password:
        raise AppError(422,'telegram_url','Usa el origen HTTPS público incluido en APP_ALLOWED_HOSTS.')
    secret=settings.telegram_webhook_secret.get_secret_value()
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}',secret):
        raise AppError(422,'telegram_secret','TELEGRAM_WEBHOOK_SECRET debe tener de 32 a 256 caracteres alfanuméricos, guiones o guiones bajos.')
    if not settings.telegram_bot_token.get_secret_value() or not settings.telegram_allowed_user_ids:
        raise AppError(422,'telegram_configuration','Faltan el token del bot o los usuarios autorizados.')
    if len(base64.b64decode(settings.billing_encryption_key.get_secret_value(),altchars=b'-_',validate=True))!=32:
        raise AppError(422,'billing_key','Configura una clave de cifrado válida antes de activar Telegram.')
    if not args.apply:
        print('Configuración comprobada sin llamadas externas. Usa --apply únicamente cuando autorices registrar el webhook.')
        return
    client=TelegramClient(settings)
    try:
        await client._call('setWebhook',json={'url':args.url.rstrip('/')+'/integrations/telegram/webhook',
            'secret_token':secret,'allowed_updates':['message','callback_query'],'drop_pending_updates':False,'max_connections':4})
        print('Webhook configurado. No se enviaron correos ni se facturaron tickets.')
    finally:
        await client.close()


if __name__=='__main__':
    try:
        asyncio.run(run())
    except (ValueError,TypeError):
        raise SystemExit('Revisa la configuración de cifrado; no se ha mostrado ningún valor secreto.') from None
    except AppError as error:
        raise SystemExit(error.message) from None
