"""Prepara el proxy del subdominio. Solo --apply escribe y recarga LiteSpeed.

Python 3.9+, sin dependencias. No lee credenciales de la aplicación.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import urllib.request
import uuid

if __package__:
    from .configure_hostinger import valid_domain
else:
    from configure_hostinger import valid_domain


BEGIN = '# BEGIN lilyapp-agent proxy v1'
END = '# END lilyapp-agent proxy v1'
SAFE_LOG = '  logFormat               "%h %l %u %t %m %U %H %>s %b"'


class ConfigurationError(Exception):
    pass


def proxy_block(domain):
    if not valid_domain(domain):
        raise ConfigurationError('Dominio no válido.')
    return f'''{BEGIN}
extprocessor lilyapp_agent {{
  type                    proxy
  address                 127.0.0.1:18000
  maxConns                10
  initTimeout             60
  retryTimeout            0
  respBuffer              0
}}

context / {{
  type                    proxy
  handler                 lilyapp_agent
  addDefaultCharset       off
  module cache {{
    enableCache           0
    enablePrivateCache    0
  }}
}}
{END}
'''


def render(original, domain):
    block = proxy_block(domain)
    if BEGIN in original or END in original:
        if original.count(BEGIN) != 1 or original.count(END) != 1 or block not in original:
            raise ConfigurationError('El bloque del agente ya existe y fue modificado; revísalo antes de continuar.')
        return original
    if re.search(r'''^\s*context\s+(?:/|"/"|'/')\s*\{''', original, re.M | re.I):
        raise ConfigurationError('El sitio ya tiene un contexto raíz. No se ha reemplazado.')
    if re.search(r'^\s*extprocessor\s+lilyapp_agent\s*\{', original, re.M | re.I):
        raise ConfigurationError('Ya existe un proxy con este nombre. No se ha reemplazado.')
    if not re.search(r'^\s*context\s+/\.well-known/acme-challenge\s*\{', original, re.M):
        raise ConfigurationError('No se encontró la ruta ACME esperada de CyberPanel.')
    if not re.search(r'^\s*vhssl\s*\{', original, re.M):
        raise ConfigurationError('No se encontró la configuración TLS del sitio.')
    # No registrar query strings OAuth, cookies, referer ni cabeceras privadas.
    pattern = r'(^[ \t]*accesslog\b[^\n]*\{\n)(.*?)(^[ \t]*\})'
    matches = list(re.finditer(pattern, original, re.M | re.S | re.I))
    if len(matches) != 1:
        raise ConfigurationError('No se encontró un único accesslog del sitio; se necesita revisión manual.')
    match = matches[0]
    body = match.group(2)
    body, count = re.subn(r'^[ \t]*logFormat[^\n]*$', lambda _: SAFE_LOG, body, flags=re.M | re.I)
    if count != 1:
        raise ConfigurationError('El formato del accesslog no coincide con el esperado.')
    body, count = re.subn(r'^[ \t]*logHeaders[^\n]*$', '  logHeaders              0', body, flags=re.M | re.I)
    if count != 1:
        raise ConfigurationError('La configuración de cabeceras del accesslog requiere revisión.')
    cleaned = original[:match.start(2)] + body + original[match.end(2):]
    # Las reglas de un contexto proxy no se ejecutan como las del virtual host.
    # Aplicar HTTPS aquí y excluir expresamente el contexto ACME más específico.
    rewrites = list(re.finditer(r'(^rewrite[ \t]*\{\n)(.*?)(^\})', cleaned, re.M | re.S))
    if len(rewrites) != 1:
        raise ConfigurationError('No se encontró el bloque rewrite del virtual host esperado.')
    rewrite = rewrites[0]
    body = rewrite.group(2)
    if re.search(r'\b(?:rules|RewriteRule|RewriteCond)\b', body, re.I) or not re.search(r'^\s*enable\s+1\s*$', body, re.M):
        raise ConfigurationError('El sitio tiene reglas de reescritura propias; se necesita revisión manual.')
    rules = rf'''  # lilyapp-agent: HTTPS; conservar la renovación ACME por HTTP.
  rules                   <<<END_LILYAPP_RULES
RewriteCond %{{HTTPS}} !=on
RewriteCond %{{REQUEST_URI}} !^/\.well-known/acme-challenge(?:/|$)
RewriteRule ^ https://{domain}%{{REQUEST_URI}} [R=301,L]
END_LILYAPP_RULES
'''
    cleaned = cleaned[:rewrite.start(2)] + body + rules + cleaned[rewrite.end(2):]
    return cleaned.rstrip() + '\n\n' + block


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def health(url, domain):
    # No enviar el chequeo local a proxies configurados en el entorno.
    handlers = [NoRedirect()]
    if url.startswith('http://127.0.0.1:18000/'):
        handlers.insert(0, urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={'Host': domain})
    with opener.open(request, timeout=5) as response:
        result = json.loads(response.read(4096))
        if response.status != 200 or result.get('status') != 'ok':
            raise ConfigurationError('El chequeo de salud no respondió correctamente.')


def checked_command(arguments):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ConfigurationError('No se pudo ejecutar la validación o recarga de LiteSpeed.') from None
    if result.returncode or '[ERROR]' in result.stdout + result.stderr:
        raise ConfigurationError('LiteSpeed no confirmó la validación o recarga. Revisa lshttpd -t antes de continuar.')


def atomic_write(path, content):
    metadata = path.stat()
    descriptor, temporary = tempfile.mkstemp(prefix='.lilyapp-', dir=path.parent)
    try:
        os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode))
        os.fchown(descriptor, metadata.st_uid, metadata.st_gid)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply(path, original, updated, domain, validator, reload_server, probe):
    if path.read_text(encoding='utf-8') != original:
        raise ConfigurationError('La configuración cambió durante la revisión. Repite la comprobación.')
    backup = path.with_name(path.name + '.lilyapp-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.bak')
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    print('Copia privada de seguridad: ' + str(backup), flush=True)
    if path.read_text(encoding='utf-8') != original:
        raise ConfigurationError('La configuración cambió al guardar la copia. Se ha conservado la edición más reciente.')
    reloaded = False
    try:
        atomic_write(path, updated)
        validator()
        # Una recarga parcial también exige intentar restaurar el estado activo.
        reloaded = True
        reload_server()
        for attempt in range(5):
            try:
                probe('https://' + domain + '/health', domain)
                break
            except Exception:
                if attempt == 4:
                    raise ConfigurationError('No se confirmó /health por HTTPS tras la recarga.') from None
                time.sleep(1)
    except BaseException:
        atomic_write(path, original)
        try:
            validator()
            if reloaded:
                reload_server()
        except Exception:
            raise ConfigurationError('Se restauró el archivo, pero no se confirmó la recarga de retorno. Revisa LiteSpeed; conserva la copia indicada.') from None
        raise ConfigurationError('No se completó el cambio. Se restauró la configuración anterior; conserva la copia indicada.') from None
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain', default='agente.lilyapp.mx')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    domain = args.domain.strip().lower()
    if not valid_domain(domain):
        raise ConfigurationError('Introduce el dominio, sin esquema, puerto ni ruta.')
    path = Path('/usr/local/lsws/conf/vhosts') / domain / 'vhost.conf'
    if path.is_symlink() or not path.is_file() or path.parent.is_symlink():
        raise ConfigurationError('No se encontró un archivo normal de configuración para este dominio.')
    original = path.read_text(encoding='utf-8')
    updated = render(original, domain)
    validator = lambda: checked_command(['/usr/local/lsws/bin/lshttpd', '-t'])
    reload_server = lambda: checked_command(['/usr/local/lsws/bin/lswsctrl', 'reload'])
    validator()
    try:
        health('http://127.0.0.1:18000/health', domain)
    except Exception:
        raise ConfigurationError('El agente local no respondió. No se ha cambiado la configuración.') from None
    if updated == original:
        health('https://' + domain + '/health', domain)
        print('El proxy ya está configurado y /health responde por HTTPS. No se cambió nada.')
        return
    if not args.apply:
        print('Comprobación completada. Solo --apply hace una copia, modifica el sitio y recarga LiteSpeed.')
        return
    if os.geteuid() != 0:
        raise ConfigurationError('Para aplicar el cambio se necesita una cuenta administradora del VPS.')
    apply(path, original, updated, domain, validator, reload_server, health)
    print('Proxy configurado; /health respondió por HTTPS. ACME y certificado conservados.')


if __name__ == '__main__':
    try:
        main()
    except (ConfigurationError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
