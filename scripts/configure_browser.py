"""Compatibilidad del activador anterior. Los permisos ahora se dan en Telegram."""
import ipaddress
import re
import sys
from urllib.parse import urlsplit


def domains(value):
    result=[]
    for part in value.split(','):
        uri=urlsplit(part.strip())
        if uri.scheme!='https' or not uri.hostname or '.' not in uri.hostname or uri.username or uri.password or uri.port not in {None,443} or uri.query or uri.fragment or uri.path not in {'','/'}:
            raise ValueError('Usa dominios HTTPS sin rutas, consultas, claves o comodines.')
        host=uri.hostname.encode('idna').decode().lower()
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',host) or host.endswith(('.local','.internal','.localhost')):
            raise ValueError('Se requieren dominios públicos concretos.')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError('Introduce el nombre de dominio del comercio, no una IP.')
        result.append('https://'+host)
    if not 1<=len(result)<=12:
        raise ValueError('Autoriza entre uno y doce dominios concretos.')
    return ','.join(dict.fromkeys(result))


def main():
    print('Ya no necesitas configurar dominios en Termius. Autoriza cada portal con el botón Permitir en Telegram.')
    print('Este script no cambió archivos ni reinició servicios. Activa el navegador con compose.browser.yaml.')


if __name__=='__main__':
    try:
        raise SystemExit(main() or 0)
    except (ValueError,OSError,EOFError):
        print('No se guardó la configuración. Revisa dominios y permisos; los secretos no se muestran.',file=sys.stderr)
        raise SystemExit(1) from None
