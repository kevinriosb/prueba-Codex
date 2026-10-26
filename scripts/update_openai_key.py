"""Sustituye solo AI_API_KEY en .env.hostinger, con entrada oculta y copia privada.

Python 3.9+, sin dependencias. No llama a APIs ni reinicia servicios.
"""
import getpass
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import uuid
import warnings


ROOT = Path(__file__).resolve().parents[1]


def read_private(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError('Se necesita un archivo .env.hostinger normal, sin enlaces.')
        if metadata.st_mode & 0o077:
            raise ValueError('El archivo no tiene permisos privados. Revisa sus permisos antes de continuar.')
        return stream.read(), metadata


def replace_value(original, token):
    # El formato no garantiza aceptación: se comprueba después, sin generación.
    if not re.fullmatch(r'sk-[A-Za-z0-9_-]{16,509}', token):
        raise ValueError('Pega la clave completa de OpenAI, que empieza por sk-, sin comillas, espacios ni prefijos.')
    lines = original.splitlines(keepends=True)
    candidates = [index for index, line in enumerate(lines)
                  if re.match(rb'^[ \t]*(?:export[ \t]+)?AI_API_KEY(?:[ \t]*=|[ \t]*(?:\r?\n|$))', line, re.I)]
    if len(candidates) != 1 or not lines[candidates[0]].startswith(b'AI_API_KEY='):
        raise ValueError('Se necesita una única línea AI_API_KEY= en el archivo; no se ha sustituido.')
    index = candidates[0]
    line = lines[index]
    ending = b'\r\n' if line.endswith(b'\r\n') else (b'\n' if line.endswith(b'\n') else b'')
    lines[index] = b'AI_API_KEY=' + token.encode('ascii') + ending
    return b''.join(lines)


def update_key(path, token, expected):
    return write_updated(path, replace_value(expected, token), expected, label='openai')


def write_updated(path, updated, expected, *, label):
    """Escritura privada común a los asistentes, con control de cambios y copia."""
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,20}', label):
        raise ValueError('Etiqueta de configuración no válida.')
    original, metadata = read_private(path)
    if original != expected:
        raise ValueError('El archivo cambió mientras introducías la clave. Se ha conservado; repite el asistente.')
    if updated == original:
        return None
    backup = path.with_name(path.name + '.' + label + '-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.bak')
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor, temporary = tempfile.mkstemp(prefix='.env.hostinger.openai-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            temporary_metadata = os.fstat(stream.fileno())
            if (metadata.st_uid, metadata.st_gid) != (temporary_metadata.st_uid, temporary_metadata.st_gid):
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        current, current_metadata = read_private(path)
        if current != original or (current_metadata.st_dev, current_metadata.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError('El archivo cambió antes de guardar. Se ha conservado la edición más reciente.')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def main():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('Ejecuta el asistente directamente en Termius, sin tuberías ni redirecciones.')
    path = ROOT / '.env.hostinger'
    original, _ = read_private(path)
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            token = getpass.getpass('Clave completa de OpenAI (oculta; Enter cancela): ')
        except getpass.GetPassWarning:
            raise ValueError('La terminal no permite ocultar la entrada. No se solicitó una clave visible.') from None
    if not token:
        print('Cancelado. No se ha modificado ningún archivo.')
        return 1
    backup = update_key(path, token, original)
    if backup is None:
        print('La clave introducida coincide con la guardada. No se ha modificado ningún archivo.')
    else:
        print('Actualizada únicamente AI_API_KEY. Copia privada anterior: ' + backup.name)
    print('Contraseña, clave de cifrado y demás variables conservadas. No se ha llamado a APIs ni reiniciado servicios.')
    print('Recrea solo el servicio agent para que lea la configuración y repite el diagnóstico sin generación.')
    return 0


def cli():
    try:
        return main()
    except ValueError as error:
        print(str(error))
    except (EOFError, KeyboardInterrupt):
        print('\nConfiguración interrumpida. No se muestran valores privados.')
    except Exception:
        # Nunca imprimir excepciones de archivos o entradas privadas.
        print('No se pudo completar el cambio. Conserva el archivo y sus copias privadas; no compartas su contenido.')
    return 1


if __name__ == '__main__':
    raise SystemExit(cli())
