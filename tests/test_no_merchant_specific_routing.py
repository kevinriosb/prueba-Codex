"""Impide volver a introducir rutas o plantillas por comercio en producción."""
import ast
from pathlib import Path
import re
from urllib.parse import urlsplit


ROOT=Path(__file__).resolve().parents[1]
# Servicios técnicos ya existentes, no entradas ni reglas para facturar en comercios.
INFRASTRUCTURE={'www.sat.gob.mx','consultaqr.facturaelectronica.sat.gob.mx','schemas.xmlsoap.org','tempuri.org',
    'www.googleapis.com','accounts.google.com','oauth2.googleapis.com','gmail.googleapis.com','api.telegram.org','browser','127.0.0.1'}
EXAMPLES=('zetus','albat','alsea','interfactura','reafacturacion','qrplus',"domino's",'dominos','starbucks','burger king','la casa de toño')


def test_no_merchant_names_or_fixed_commercial_hosts_in_production():
    problems=[]
    for directory in ('backend/app','backend/browser_worker'):
        for path in (ROOT/directory).rglob('*.py'):
            source=path.read_text()
            for word in EXAMPLES:
                if word in source.casefold():problems.append(f'{path.relative_to(ROOT)} contiene {word}')
            # También bloquea dominios de comercios nuevos, no solo los ejemplos.
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node,ast.Constant) or not isinstance(node.value,(str,bytes)):continue
                value=node.value.decode('utf-8','ignore') if isinstance(node.value,bytes) else node.value
                for address in re.findall(r'https?://[a-zA-Z0-9_.:-]+',value):
                    host=urlsplit(address).hostname
                    if host and host!='.' and host not in INFRASTRUCTURE:
                        problems.append(f'{path.relative_to(ROOT)}:{node.lineno} dominio fijo {host}')
    assert problems==[],'\n'.join(problems)
