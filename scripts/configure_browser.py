"""Autoriza dominios públicos sin cambiar contraseñas, cifrado o cuotas. Python 3.9+."""
import ipaddress
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

if __package__:
    from .configure_hostinger_telegram import render_variables
    from .update_openai_key import read_private,write_updated
else:
    from configure_hostinger_telegram import render_variables
    from update_openai_key import read_private,write_updated


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
    path=Path(__file__).resolve().parents[1]/'.env.hostinger'
    original,_=read_private(path)
    print('Autoriza solo los dominios públicos que revisarás. No se envían datos fiscales.')
    value=input('Dominios HTTPS, separados por coma (Enter cancela): ').strip()
    if not value:
        print('Cancelado; configuración conservada.')
        return 1
    encoded=domains(value)
    updated=render_variables(original,{'BILLING_BROWSER_ORIGINS':encoded})
    backup=write_updated(path,updated,original,label='browser')
    print('Dominios autorizados: '+encoded)
    print('Configuración privada guardada'+('; copia anterior: '+backup.name if backup else '; sin cambios')+'.')
    print('No se reiniciaron servicios ni se llamó a OpenAI o a comercios.')


if __name__=='__main__':
    try:
        raise SystemExit(main() or 0)
    except (ValueError,OSError,EOFError):
        print('No se guardó la configuración. Revisa dominios y permisos; los secretos no se muestran.',file=sys.stderr)
        raise SystemExit(1) from None
