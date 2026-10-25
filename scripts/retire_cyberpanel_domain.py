"""Desactiva referencias de un dominio obsoleto, conservando todos sus archivos.

Por defecto solo presenta el plan. --apply respalda y prepara el archivo global,
valida el resultado y no recarga LiteSpeed. Python 3.9+, sin dependencias.
"""
import argparse
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import time
import uuid

if __package__:
    from .configure_cyberpanel_proxy import ConfigurationError, atomic_write, checked_command
    from .configure_hostinger import valid_domain
else:
    from configure_cyberpanel_proxy import ConfigurationError, atomic_write, checked_command
    from configure_hostinger import valid_domain


SERVER_CONFIG = Path('/usr/local/lsws/conf/httpd_config.conf')


@dataclass
class Block:
    kind: str
    name: str
    start: int
    end: int


def blocks(lines):
    result, depth, current = [], 0, None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        opening = re.fullmatch(r'([A-Za-z]\w*)(?:[ \t]+([^{}]+?))?[ \t]*\{[ \t]*(?:#.*)?', stripped)
        closing = re.fullmatch(r'\}[ \t]*(?:#.*)?', stripped)
        if opening:
            if depth == 0:
                current = (opening.group(1).lower(), (opening.group(2) or '').strip().strip('"\''), index)
            depth += 1
        elif closing:
            depth -= 1
            if depth < 0:
                raise ConfigurationError('El archivo global tiene llaves no reconocidas; se necesita revisión manual.')
            if depth == 0:
                result.append(Block(*current, index + 1))
                current = None
    if depth:
        raise ConfigurationError('El archivo global tiene bloques incompletos; no se ha cambiado.')
    return result


def retired(name, domain):
    name = name.lower().rstrip('.').removeprefix('*.')
    return name == domain or name.endswith('.' + domain)


def directive(lines, key):
    matches = [re.match(r'^[ \t]*' + re.escape(key) + r'[ \t]+(.+?)\s*$', line, re.I) for line in lines]
    values = [shlex.split(match.group(1), comments=True) for match in matches if match]
    if len(values) != 1 or len(values[0]) != 1:
        raise ConfigurationError('El listener tiene un campo ' + key + ' ambiguo o ausente.')
    return values[0][0]


def map_entry(line):
    match = re.match(r'^[ \t]*map[ \t]+(.+?)\s*$', line, re.I)
    if not match:
        return None
    tokens = shlex.split(match.group(1), comments=True)
    if len(tokens) < 2:
        raise ConfigurationError('Se encontró un mapping incompleto; se necesita revisión manual.')
    return tokens[0], [item.lower().rstrip('.') for item in re.split(r'[,\s]+', ' '.join(tokens[1:])) if item]


def protected_routes(lines, parsed, hosts):
    snapshot = {}
    definitions = {}
    for block in parsed:
        if block.kind == 'virtualhost':
            definitions.setdefault(block.name.lower(), []).append(''.join(lines[block.start:block.end]))
    for host in hosts:
        routes = []
        for block in parsed:
            if block.kind != 'listener':
                continue
            for line in lines[block.start:block.end]:
                entry = map_entry(line)
                if entry and (host in entry[1] or '*' in entry[1]):
                    target = entry[0].lower()
                    if target not in definitions:
                        raise ConfigurationError('Una ruta protegida apunta a un virtual host no definido.')
                    routes.append((block.name, line, tuple(definitions[target])))
        if not routes:
            raise ConfigurationError('No se encontró la ruta del sitio protegido ' + host + '.')
        snapshot[host] = routes
    return snapshot


def old_certificate(path, domain):
    parts = PurePosixPath(path).parts
    return len(parts) >= 6 and parts[:4] == ('/', 'etc', 'letsencrypt', 'live') and retired(parts[4], domain)


def render(original, domain, keep_hosts):
    domain = domain.lower().rstrip('.')
    if not valid_domain(domain) or not keep_hosts or any(not valid_domain(host) or retired(host, domain) for host in keep_hosts):
        raise ConfigurationError('Indica el dominio obsoleto y sitios protegidos válidos, separados de ese dominio.')
    lines = original.splitlines(keepends=True)
    parsed = blocks(lines)
    before = protected_routes(lines, parsed, keep_hosts)
    removed_blocks, removed_maps, changed_certificates = [], 0, []
    skip, replacements = set(), {}
    listeners = [block for block in parsed if block.kind == 'listener']
    for block in parsed:
        if block.kind == 'virtualhost' and retired(block.name, domain):
            removed_blocks.append(block.name)
            skip.update(range(block.start, block.end))
    sources = set()
    for block in listeners:
        body = lines[block.start:block.end]
        if directive(body, 'secure') != '1':
            continue
        key, cert = directive(body, 'keyFile'), directive(body, 'certFile')
        routes = [map_entry(line) for line in body]
        has_protected_route = any(entry and any(host in entry[1] or '*' in entry[1] for host in keep_hosts) for entry in routes)
        if has_protected_route and not old_certificate(key, domain) and not old_certificate(cert, domain):
            sources.add((key, cert))
    for block in listeners:
        body = lines[block.start:block.end]
        for index in range(block.start, block.end):
            entry = map_entry(lines[index])
            if not entry:
                continue
            target, aliases = entry
            if retired(target, domain):
                if not all(retired(alias, domain) for alias in aliases):
                    raise ConfigurationError('Un virtual host antiguo atiende otros dominios; no se desactivó.')
                skip.add(index)
                removed_maps += 1
            elif any(retired(alias, domain) for alias in aliases):
                raise ConfigurationError('Hay aliases antiguos mezclados con una ruta activa; se necesita revisión.')
        if directive(body, 'secure') != '1':
            continue
        key, cert = directive(body, 'keyFile'), directive(body, 'certFile')
        is_old_key, is_old_cert = old_certificate(key, domain), old_certificate(cert, domain)
        if not is_old_key and not is_old_cert:
            continue
        if is_old_key != is_old_cert or len(sources) != 1:
            raise ConfigurationError('No se pudo elegir un par de certificados de respaldo inequívoco; no se cambió.')
        new_key, new_cert = next(iter(sources))
        for index in range(block.start, block.end):
            match = re.match(r'^([ \t]*)(keyFile|certFile)[ \t]+[^\n]*', lines[index], re.I)
            if match:
                field = match.group(2)
                replacements[index] = match.group(1) + field + '                 ' + (new_key if field.lower() == 'keyfile' else new_cert) + '\n'
        changed_certificates.append((block.name, new_key, new_cert))
    updated = ''.join(replacements.get(index, line) for index, line in enumerate(lines) if index not in skip)
    new_lines = updated.splitlines(keepends=True)
    if protected_routes(new_lines, blocks(new_lines), keep_hosts) != before:
        raise ConfigurationError('Cambiarían las rutas protegidas; se ha cancelado el plan.')
    return updated, {'virtual_hosts': removed_blocks, 'mappings': removed_maps, 'certificates': changed_certificates}


def stage(path, original, updated, validator):
    if path.read_text(encoding='utf-8') != original:
        raise ConfigurationError('La configuración cambió desde la revisión; repite la comprobación.')
    backup = path.with_name(path.name + '.lilyapp-retired-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.bak')
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    print('Copia privada del archivo global: ' + str(backup), flush=True)
    if path.read_text(encoding='utf-8') != original:
        raise ConfigurationError('La configuración cambió al crear la copia; no se ha sustituido.')
    try:
        atomic_write(path, updated)
        validator()
    except BaseException:
        atomic_write(path, original)
        raise ConfigurationError('La propuesta no pasó la validación. Se restauró el archivo global; no se recargó LiteSpeed.') from None
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retire-domain', required=True)
    parser.add_argument('--keep-hosts', required=True, help='Sitios actuales que se conservan, separados por comas.')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    domain = args.retire_domain.strip().lower()
    keep_hosts = [host.strip().lower() for host in args.keep_hosts.split(',') if host.strip()]
    path = SERVER_CONFIG
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError('No se encontró un archivo global normal de LiteSpeed.')
    original = path.read_text(encoding='utf-8')
    updated, plan = render(original, domain, keep_hosts)
    print('Definiciones antiguas a desactivar: ' + str(len(plan['virtual_hosts'])) + '; rutas: ' + str(plan['mappings'])
          + '; listeners con certificado de respaldo actualizado: ' + str(len(plan['certificates'])) + '.')
    print('Sitios protegidos: ' + ', '.join(keep_hosts) + '. No se eliminan archivos, certificados ni contenedores.')
    validator = lambda: checked_command(['/usr/local/lsws/bin/lshttpd', '-t'], allow_warnings=True)
    if not args.apply:
        print('Plan preparado sin cambios. Solo --apply respalda y prepara la configuración; no recarga LiteSpeed.')
        return
    if os.geteuid() != 0:
        raise ConfigurationError('Para preparar el cambio se necesita una cuenta administradora.')
    for _, key, cert in plan['certificates']:
        if not Path(key).is_file() or not Path(cert).is_file():
            raise ConfigurationError('Los archivos del certificado de respaldo no existen; no se cambió nada.')
    if updated == original:
        validator()
        print('Las referencias ya están desactivadas. No se cambió nada.')
        return
    stage(path, original, updated, validator)
    print('Archivo global preparado y validado. No se recargó LiteSpeed. Continúa con la configuración del proxy del agente.')


if __name__ == '__main__':
    try:
        main()
    except (ConfigurationError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
