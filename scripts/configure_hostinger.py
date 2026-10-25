"""Crea una configuración privada inicial; no instala, despliega ni llama a APIs.

Compatible con Python 3.9+ del servidor AlmaLinux; el agente usa Python 3.12 en Docker.
"""
import base64
import getpass
import os
from pathlib import Path
import re
import secrets
import sys
import warnings


ROOT = Path(__file__).resolve().parents[1]


def valid_domain(value):
    if len(value) > 253 or '.' not in value:
        return False
    return all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
               for label in value.split('.'))


def build_configuration(domain, username, password, api_key, template):
    domain = domain.strip().lower()
    if not valid_domain(domain):
        raise ValueError('Introduce un dominio válido, sin https://, puerto ni ruta.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', username):
        raise ValueError('El usuario necesita 3–64 letras, números, puntos, guiones o guiones bajos.')
    if not 12 <= len(password) <= 256:
        raise ValueError('La contraseña necesita entre 12 y 256 caracteres.')
    if any(character in password + api_key for character in '\r\n\x00'):
        raise ValueError('Los valores privados deben ocupar una sola línea.')
    if api_key != api_key.strip():
        raise ValueError('La clave de OpenAI tiene espacios exteriores; vuelve a introducirla.')
    values = {
        'APP_LOGIN_USERNAME': username,
        'APP_LOGIN_PASSWORD': password,
        'APP_ALLOWED_HOSTS': domain + ',localhost,127.0.0.1',
        'AI_API_KEY': api_key,
        'BILLING_ENCRYPTION_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii'),
        'BILLING_LIVE_EMAIL_ENABLED': 'false',
        'BILLING_SAT_ENABLED': 'false',
        'BILLING_BACKGROUND_TRACKING_ENABLED': 'false',
    }
    result = []
    for line in template.splitlines():
        key, separator, _ = line.partition('=')
        result.append(key + '=' + values[key] if separator and key in values else line)
    return '\n'.join(result) + '\n'


def write_new_configuration(destination, content):
    # O_EXCL rechaza archivos y enlaces existentes: nunca sustituye una clave anterior.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def main():
    destination = ROOT / '.env.hostinger'
    if destination.exists() or destination.is_symlink():
        raise SystemExit('Ya existe .env.hostinger. Consérvalo: no se ha cambiado ninguna clave.')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit('Ejecuta este asistente directamente en una terminal interactiva, sin tuberías ni redirecciones.')
    domain = input('Dominio del agente [agente.lilyapp.mx]: ').strip() or 'agente.lilyapp.mx'
    username = input('Usuario de acceso [admin]: ').strip() or 'admin'
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            password = getpass.getpass('Contraseña nueva de la aplicación (12–256 caracteres, oculta): ')
            repeated = getpass.getpass('Repite la contraseña: ')
            if password != repeated:
                raise SystemExit('Las contraseñas no coinciden. No se ha creado ningún archivo.')
            api_key = getpass.getpass('Clave OpenAI (oculta; Enter para configurarla después): ')
        except getpass.GetPassWarning:
            raise SystemExit('Esta terminal no permite ocultar las entradas. Abre una terminal SSH interactiva.') from None
    try:
        content = build_configuration(domain, username, password, api_key,
                                      (ROOT / '.env.hostinger.example').read_text(encoding='utf-8'))
        write_new_configuration(destination, content)
    except FileExistsError:
        raise SystemExit('Otra ejecución ya creó .env.hostinger. Se ha conservado su contenido.') from None
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print('Creado .env.hostinger con permisos privados. No se ha desplegado ni llamado a ninguna API.')
    print('Guarda una copia segura de este archivo: contiene la clave necesaria para recuperar los datos cifrados.')
    print('Correo, consultas SAT y seguimiento automático permanecen desactivados.')


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit('\nConfiguración cancelada.') from None
